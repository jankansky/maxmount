# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI gateway (`mount-svc gui`): serves the web GUI and bridges a WebSocket to Zenoh.

The browser never talks to Zenoh, mount-rt or mount-svc directly; this is the one place where
authentication and a single-controller lock will go. For now it is meant for the local network
only (no authentication): bind it to the observatory LAN, and use a VPN for remote access.

WebSocket messages (JSON):
  server -> client
    {"type": "hello", ...}            site, axes, limits, park position, tracking settings
    {"type": "telemetry", ...}        10 Hz: drive states, axis angles, sky position, loop, bus
    {"type": "status", ...}           mount-svc ServiceStatus (2 Hz)
    {"type": "groundtrack", ...}      sub-satellite track and visibility radius for the target
    {"type": "reply", "id", "ok", "message", "data"}
  client -> server
    {"type": "request", "id", "op", "args"}
      op: target (TargetCommand as JSON), stop, hold, park, tle_names,
          passes {group, names | all, hours, min_max_el_deg}, pass_plan {name, line1, line2, rise_ns, set_ns},
          pass_preview {name, line1, line2, rise_ns, culmination_ns, set_ns} (ground track + sky path),
          conjunctions {hours, max_range_km, lighting, max_mag, coorbiting} (SOCRATES, visible from the site),
          aircraft {max_range_km, show, selected} (local ADS-B receiver: list, map, sky, the selected one's path),
          set_imaging {profile} (the active narrow-field imaging profile; broadcast as {"type": "imaging"}),
          camera {camera_id, set {name, value} | capture {bulb_s} | liveview | save_to | stretch | abort
                  | record {on, duration_s, max_frames}}
            (to camera/<id>/cmd; status arrives as {"type": "camera"}, frames as {"type": "camframe"}),
          conjunction_detail {cat1, cat2, primary, tca_ns} (both paths, separation, the pass to track),
          enable/disable {axis} and fault_reset (straight to mount-rt),
          jog {axis, by_deg | to_deg + absolute, vel_deg_s, acc_deg_s2} (through mount-svc)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import time
import urllib.request
from pathlib import Path

import erfa
import numpy as np
from aiohttp import WSMsgType, web
from google.protobuf import json_format

from . import adsb, astro, kinematics, planner, satcat, socrates
from .config import SystemSettings
from .mount import MountClient, MountError
from .proto import mpb
from .targets import SatelliteTarget, SgpError

log = logging.getLogger(__name__)
DEG = math.pi / 180
NS = 1_000_000_000
REPO = Path(__file__).resolve().parents[3]
STATIC_DIR = REPO / "gui"
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
TLE_GROUPS = ("stations", "visual", "active", "starlink", "weather", "science")
TLE_MAX_AGE_S = 6 * 3600
ALL_MAX = 400          # largest group searched with "all objects"
PASS_ROWS_MAX = 300    # passes returned to the GUI
LIVEVIEW_MAX_FPS = 8   # live view frames forwarded to browsers
COORBIT_KM_S = 0.1     # conjunctions slower than this are formation flyers or co-located


def _msg_to_dict(m) -> dict:
    return json_format.MessageToDict(m, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)


WATCH_MAX = 30         # objects one watch-list term may match


def _norad(line1: str) -> str:
    return line1[2:7].strip().lstrip("0")


def _match_names(sats: dict, terms: list[str]):
    """Watch-list terms -> object names. A term matches, in order of preference: the exact
    name, a catalog number, or every name containing it (case-insensitive), so 'ISS' finds
    'ISS (ZARYA)' and 'ISS (NAUKA)'."""
    out, notes = [], {"missing": [], "capped": "", "count": 0}
    for term in (t.strip() for t in terms):
        if not term:
            continue
        if term in sats:
            found = [term]
        elif term.isdigit():
            found = [n for n, (l1, _l2) in sats.items() if _norad(l1) == term.lstrip("0")]
        else:
            found = [n for n in sats if term.casefold() in n.casefold()]
        if not found:
            notes["missing"].append(term)
        elif len(found) > WATCH_MAX:
            notes.update(capped=term, count=len(found))
            return [], notes
        out += [n for n in found if n not in out]
    return out, notes


def _lit_enough(row: dict, lighting: str) -> bool:
    if lighting == "any":
        return True
    lit = (row.get("sunlit_frac") or 0) > 0
    return lit and (lighting == "sunlit" or (row["sun_el_deg"] is not None and row["sun_el_deg"] < -6))


def _merge_docked(rows: list[dict]) -> list[dict]:
    """Objects docked together (ISS modules, CSS modules) have separate element sets with the
    same passes; keep one row (lowest catalog number) and list the others in 'also'."""
    rows = sorted(rows, key=lambda r: (int(r["rise_ns"]), int(_norad(r["line1"]) or 0)))
    out: list[dict] = []
    for r in rows:
        for k in out[-8:]:
            if (abs(int(k["rise_ns"]) - int(r["rise_ns"])) < 15 * NS and abs(int(k["set_ns"]) - int(r["set_ns"])) < 15 * NS
                    and abs(k["max_el_deg"] - r["max_el_deg"]) < 0.5):
                k.setdefault("also", []).append(r["name"])
                break
        else:
            out.append(r)
    return out


class TleCache:
    """CelesTrak element sets by group, cached on disk for offline use."""

    def __init__(self):
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.dir = base / "maxmount" / "tle"

    def group(self, group: str) -> tuple[dict[str, tuple[str, str]], str]:
        if group not in TLE_GROUPS:
            raise ValueError(f"unknown TLE group '{group}'")
        path = self.dir / f"{group}.txt"
        note = "cached"
        if not path.exists() or time.time() - path.stat().st_mtime > TLE_MAX_AGE_S:
            try:
                req = urllib.request.Request(CELESTRAK.format(group=group), headers={"User-Agent": "maxmount"})
                with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https URL
                    data = r.read().decode("ascii", "replace")
                if "1 " not in data:
                    raise OSError("no element sets in the reply")
                self.dir.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
                note = "downloaded"
            except OSError as e:
                # CelesTrak answers 403 with an explanation (e.g. one download per 2 h update).
                why = e.read().decode("utf-8", "replace").strip().replace("\n", " ")[:200] if hasattr(e, "read") else ""
                if not path.exists():
                    raise RuntimeError(f"cannot fetch TLE group '{group}': {e}{': ' + why if why else ''}") from None
                log.warning("TLE download failed (%s); using the cached copy", e)
        lines = [ln.rstrip() for ln in path.read_text().splitlines() if ln.strip()]
        sats: dict[str, tuple[str, str]] = {}
        for i in range(len(lines) - 2):
            if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 ") and not lines[i].startswith(("1 ", "2 ")):
                sats[lines[i].strip()] = (lines[i + 1], lines[i + 2])
        age_h = (time.time() - path.stat().st_mtime) / 3600
        return sats, f"{note}, {age_h:.1f} h old"


class ImagingState:
    """The active imaging profile, chosen in the GUI and remembered across restarts in
    $XDG_STATE_HOME/maxmount/<system>-imaging.json (default ~/.local/state)."""

    def __init__(self, settings: SystemSettings, state_dir: Path | None = None):
        self.settings = settings
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        self.path = (state_dir or base / "maxmount") / f"{settings.name}-imaging.json"
        self.key = settings.imaging.default
        try:
            saved = json.loads(self.path.read_text()).get("active")
            if saved in settings.imaging.profiles:
                self.key = saved
        except (OSError, ValueError, AttributeError):
            pass

    @property
    def active(self):
        return self.settings.imaging.profiles[self.key]

    def set(self, key: str) -> None:
        if key not in self.settings.imaging.profiles:
            raise ValueError(f"unknown imaging profile '{key}'")
        self.key = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"active": key}))

    def message(self) -> dict:
        return {"type": "imaging", "active": self.key,
                "profiles": [p.summary() for p in self.settings.imaging.profiles.values()]}


class Gateway:
    def __init__(self, settings: SystemSettings, host: str = "0.0.0.0", port: int = 8080, static_dir: Path = STATIC_DIR,
                 park_az_deg: float = 0.0, park_el_deg: float = 45.0):
        self.settings = settings
        self.host, self.port = host, port
        self.static_dir = Path(static_dir)
        self.park = (park_az_deg, park_el_deg)
        self.tles = TleCache()
        self.imaging = ImagingState(settings)
        self.satcat = satcat.SatCatalog()
        self.socrates = socrates.Socrates()
        self.adsb = adsb.AdsbSource(settings.adsb.source)
        self.clients: set[web.WebSocketResponse] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: MountClient | None = None
        self._latest_tel = None
        self._latest_status = None
        self._sun = None  # (az, el) deg, refreshed every 10 s
        self._sun_t = 0.0
        self._track_key = None
        self._track_msg = None
        self._subs = []
        self._runner: web.AppRunner | None = None
        self._cameras: dict[str, dict] = {}       # latest {"type": "camera", ...} per camera id
        self._last_capture: dict[str, dict] = {}  # latest capture frame per camera id
        self._lv_sent: dict[str, float] = {}      # live view frames are sent at most LIVEVIEW_MAX_FPS

    # -- Zenoh side ----------------------------------------------------------------------------
    def _on_zenoh(self, kind: str, payload: bytes) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._store, kind, payload)

    def _store(self, kind: str, payload: bytes) -> None:
        if kind == "camstatus":
            m = mpb.CameraStatus()
            m.ParseFromString(payload)
            msg = {"type": "camera", **_msg_to_dict(m)}
            self._cameras[m.camera_id] = msg
            self._broadcast(msg)
            return
        if kind == "camframe":
            self._camera_frame(payload)
            return
        if kind == "telemetry":
            m = mpb.MountTelemetry()
            m.ParseFromString(payload)
            self._latest_tel = m
        else:
            m = mpb.ServiceStatus()
            m.ParseFromString(payload)
            self._latest_status = m
            self._broadcast({"type": "status", **_msg_to_dict(m)})
            self._maybe_groundtrack(m)

    def _camera_frame(self, payload: bytes) -> None:
        """A camera frame to the browsers as {"type": "camframe", ..., "jpeg": base64}; live view
        is thinned to LIVEVIEW_MAX_FPS (WiFi), captures always go through."""
        f = mpb.CameraFrame()
        f.ParseFromString(payload)
        if f.kind == "liveview":
            now = time.monotonic()
            if now - self._lv_sent.get(f.camera_id, 0) < 1 / LIVEVIEW_MAX_FPS:
                return
            self._lv_sent[f.camera_id] = now
        msg = {"type": "camframe", "camera_id": f.camera_id, "kind": f.kind, "t_ns": str(f.t_ns), "width": f.width,
               "height": f.height, "stretched": f.stretched, "histogram": list(f.histogram), "median": f.median,
               "p99": f.p99, "saturated_frac": f.saturated_frac, "files": list(f.files), "exposure_s": f.exposure_s,
               "iso": f.iso, "shutter": f.shutter, "gain": f.gain, "bit_depth": f.bit_depth,
               "jpeg": base64.b64encode(f.jpeg).decode("ascii")}
        if f.kind == "capture":
            self._last_capture[f.camera_id] = msg
        self._broadcast(msg)

    def _camera_cmd(self, args: dict):
        cam = str(args.get("camera_id") or "dslr")
        cmd = mpb.CameraCommand()
        if "set" in args:
            cmd.set.name, cmd.set.value = str(args["set"]["name"]), str(args["set"]["value"])
        elif "capture" in args:
            cmd.capture.bulb_s = float((args["capture"] or {}).get("bulb_s", 0))
        elif "liveview" in args:
            cmd.liveview = bool(args["liveview"])
        elif "save_to" in args:
            cmd.save_to = str(args["save_to"])
        elif "stretch" in args:
            cmd.stretch = bool(args["stretch"])
        elif "abort" in args:
            cmd.abort = True
        elif "record" in args:
            r = args["record"] or {}
            cmd.record.on = bool(r.get("on", True))
            cmd.record.duration_s = float(r.get("duration_s") or 0)
            cmd.record.max_frames = int(r.get("max_frames") or 0)
        else:
            return False, "empty camera command", None
        out = self.client.query(f"camera/{cam}/cmd", cmd.SerializeToString(), timeout=20)
        if out is None:
            return False, f"no reply from the {cam} camera service: is 'mount-svc dslr' running?", None
        r = mpb.CameraReply()
        r.ParseFromString(out)
        return r.ok, r.message, None

    # -- derived quantities --------------------------------------------------------------------
    def _sky(self, az_axis: float, el_axis: float, t_ns: int) -> dict:
        """Sky position of the mount (ignoring the pointing model): az/el, ICRS RA/Dec, LST."""
        site = self.settings.site
        az, el = kinematics.from_mech(az_axis, el_axis)
        out = {"az_deg": float(np.degrees(az)), "el_deg": float(np.degrees(el))}
        if site is None:
            return out
        u1, u2 = astro.utc_jd(np.int64(t_ns))
        ut1 = erfa.utcut1(u1, u2, site.dut1_s)
        tt = astro.tt_jd(np.int64(t_ns))
        lst = (erfa.gst06a(ut1[0], ut1[1], tt[0], tt[1]) + math.radians(site.longitude_deg)) % (2 * math.pi)
        ra, dec = erfa.atoc13(
            "A", float(az), math.pi / 2 - float(el), u1, u2, site.dut1_s, math.radians(site.longitude_deg),
            math.radians(site.latitude_deg), site.height_m, 0.0, 0.0, site.pressure_hpa, site.temperature_c,
            site.humidity, site.wavelength_um,
        )
        out.update(ra_h=float(ra) * 12 / math.pi, dec_deg=float(np.degrees(dec)), lst_h=float(lst) * 12 / math.pi)
        return out

    def _sun_pos(self):
        if self.settings.site is None:
            return None
        if time.monotonic() - self._sun_t > 10:
            self._sun_t = time.monotonic()
            az, el = astro.sun_observed(np.int64(time.time_ns()), self.settings.site)
            self._sun = (float(np.degrees(az)), float(np.degrees(el)))
        return self._sun

    def _telemetry_msg(self, m) -> dict:
        axes = []
        pos = {}
        for a in m.axes:
            pos[a.axis] = a.act_position_rad
            axes.append({
                "axis": a.axis,
                "state": mpb.DriveState.Name(a.state).replace("DRIVE_STATE_", ""),
                "cmd_deg": math.degrees(a.cmd_position_rad),
                "act_deg": math.degrees(a.act_position_rad),
                "err_arcsec": math.degrees(a.cmd_position_rad - a.act_position_rad) * 3600,
                "cmd_vel_deg_s": math.degrees(a.cmd_velocity_rad_s),
                "flags": a.flags,
                "ferr_counts": a.following_error_counts,
                "di": a.digital_inputs,
                "sw": a.statusword,
                "error_code": a.error_code,
                "vendor_error": a.vendor_error_register,
                "buffered": a.buffered_points,
            })
        msg = {
            "type": "telemetry",
            "t_ns": m.t_ns,
            "enabled": m.enabled,
            "tripped": m.tripped,
            "inputs_fresh": m.inputs_fresh,
            "cycle": m.cycle,
            "axes": axes,
            "loop": {"max_lat_us": m.loop.max_wake_latency_ns / 1000, "overruns": m.loop.overruns, "cycles": m.loop.cycles},
            "bus": {"lost": m.bus.lost_frames, "wkc": m.bus.working_counter_errors},
        }
        if "az" in pos and "el" in pos:
            msg["sky"] = self._sky(pos["az"], pos["el"], m.t_ns)
        sun = self._sun_pos()
        if sun:
            msg["sun"] = {"az_deg": sun[0], "el_deg": sun[1]}
        return msg

    def _maybe_groundtrack(self, st) -> None:
        """Sub-satellite track (±50 min) and visibility radius when the target is a satellite."""
        if not st.tle_line1 or self.settings.site is None:
            return
        minute = int(time.time() // 60)
        key = (st.tle_line1, st.tle_line2, minute)
        if key == self._track_key:
            return
        self._track_key = key
        try:
            sat = SatelliteTarget(self.settings.site, st.tle_line1, st.tle_line2)
            now = time.time_ns()
            t = np.arange(now - 50 * 60 * NS, now + 50 * 60 * NS, 30 * NS, dtype=np.int64)
            lon, lat, alt = self._subsat(sat, t)
            i_now = int(np.argmin(np.abs(t - now)))
            self._track_msg = {
                "type": "groundtrack",
                "points": [[round(float(a), 2), round(float(b), 2), int(ti > now)] for a, b, ti in zip(lon, lat, t)],
                "now": [round(float(lon[i_now]), 2), round(float(lat[i_now]), 2)],
                "visibility_deg": self._visibility_deg(alt[i_now]),
                "site": [self.settings.site.longitude_deg, self.settings.site.latitude_deg],
            }
            self._broadcast(self._track_msg)
        except Exception:  # noqa: BLE001 - a ground track must not take the gateway down
            log.exception("ground track failed")

    def _subsat(self, sat: SatelliteTarget, t):
        """Sub-satellite longitude, latitude (deg) and altitude (km) at times t (ns)."""
        jd1, jd2 = astro.utc_jd(t)
        _e, r, _v = sat._sat.sgp4_array(jd1, jd2)
        pef = astro.teme_to_pef(np.asarray(r), t, self.settings.site.dut1_s)
        lon = np.degrees(np.arctan2(pef[:, 1], pef[:, 0]))
        lat = np.degrees(np.arctan2(pef[:, 2], np.hypot(pef[:, 0], pef[:, 1])))
        return lon, lat, np.linalg.norm(pef, axis=1) - 6378.137

    def _visibility_deg(self, alt_km: float) -> float:
        """Ground radius (deg of arc) from which the satellite is above the horizon mask."""
        mask = math.radians(self.settings.tracking.horizon_min_el_deg)
        re = 6378.137
        return math.degrees(math.acos(re / (re + alt_km) * math.cos(mask)) - mask)

    def _pass_preview(self, args: dict):
        """Ground track and sky path of one pass, for previewing it before tracking it."""
        if self.settings.site is None:
            return False, "no site location", None
        sat = SatelliteTarget(self.settings.site, str(args["line1"]), str(args["line2"]), name=str(args.get("name", "")))
        rise, set_ = int(args["rise_ns"]), int(args["set_ns"])
        culm = int(args.get("culmination_ns") or (rise + set_) // 2)
        # Ground track: the pass plus 15 min either side; flag 1 = above the horizon mask.
        t = np.arange(rise - 15 * 60 * NS, set_ + 15 * 60 * NS, 20 * NS, dtype=np.int64)
        t = np.unique(np.concatenate([t, [rise, culm, set_]]))
        lon, lat, alt = self._subsat(sat, t)
        i_c = int(np.searchsorted(t, culm))
        ground = {
            "points": [[round(float(a), 2), round(float(b), 2), int(rise <= ti <= set_)] for a, b, ti in zip(lon, lat, t)],
            "now": [round(float(lon[i_c]), 2), round(float(lat[i_c]), 2)],
            "aos": [round(float(lon[int(np.searchsorted(t, rise))]), 2), round(float(lat[int(np.searchsorted(t, rise))]), 2)],
            "los": [round(float(lon[int(np.searchsorted(t, set_))]), 2), round(float(lat[int(np.searchsorted(t, set_))]), 2)],
            "visibility_deg": self._visibility_deg(float(alt[i_c])),
            "site": [self.settings.site.longitude_deg, self.settings.site.latitude_deg],
        }
        # Sky path from rise to set (apparent az/el) and whether the satellite is sunlit.
        ts = np.unique(np.concatenate([np.arange(rise, set_, 5 * NS, dtype=np.int64), [set_]]))
        az, el = sat.observed(ts)
        lit = sat.sunlit(ts)
        sky = [[round(math.degrees(a), 2), round(math.degrees(e), 2), int(bool(s))] for a, e, s in zip(az, el, lit)]
        n_lit = int(np.count_nonzero(lit))
        sunlit = "sunlit" if n_lit == len(ts) else "in shadow" if n_lit == 0 else "partly sunlit"
        return True, f"preview {sat.name}: {sunlit}", {"ground": ground, "sky": sky, "sunlit": sunlit}

    # -- WebSocket ------------------------------------------------------------------------------
    def _broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(data))

    def _hello(self) -> dict:
        s = self.settings
        lim = lambda a: {"min_deg": math.degrees(a.min_rad), "max_deg": math.degrees(a.max_rad),  # noqa: E731
                         "max_vel_deg_s": math.degrees(a.max_vel), "max_acc_deg_s2": math.degrees(a.max_acc)}
        site = s.site
        return {
            "type": "hello",
            "system": s.name,
            "backend": s.backend,
            "site": None if site is None else {
                "lat_deg": site.latitude_deg, "lon_deg": site.longitude_deg, "height_m": site.height_m,
                "source": site.position_source, "dut1_s": site.dut1_s,
            },
            "limits": {"az": lim(s.az), "el": lim(s.el)},
            "tracking": {"horizon_min_el_deg": s.tracking.horizon_min_el_deg, "sun_min_sep_deg": s.tracking.sun_min_sep_deg},
            "park": {"az_deg": self.park[0], "el_deg": self.park[1]},
            "max_clock_error_us": s.max_clock_error_us,
            "imaging": self.imaging.message(),
            "tle_groups": list(TLE_GROUPS),
            "axes": s.axes_info,
            "fieldbus": {k: s.fieldbus.get(k) for k in ("backend", "interface", "cycle_us")},
        }

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(json.dumps(self._hello()))
        if self._latest_status is not None:
            await ws.send_str(json.dumps({"type": "status", **_msg_to_dict(self._latest_status)}))
        if self._track_msg:
            await ws.send_str(json.dumps(self._track_msg))
        for msg in [*self._cameras.values(), *self._last_capture.values()]:
            await ws.send_str(json.dumps(msg))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    req = json.loads(msg.data)
                except ValueError:
                    continue
                if req.get("type") == "request":
                    asyncio.ensure_future(self._handle(ws, req))
        finally:
            self.clients.discard(ws)
        return ws

    async def _handle(self, ws, req: dict) -> None:
        rid, op, args = req.get("id"), req.get("op"), req.get("args") or {}
        try:
            ok, message, data = await asyncio.get_running_loop().run_in_executor(None, self._do, op, args)
        except Exception as e:  # noqa: BLE001 - every request gets a reply
            ok, message, data = False, f"{type(e).__name__}: {e}", None
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "reply", "id": rid, "ok": ok, "message": message, "data": data}))

    def _target(self, cmd) -> tuple[bool, str, dict | None]:
        out = self.client.query("svc/target", cmd.SerializeToString(), timeout=30)
        if out is None:
            return False, "no reply from mount-svc: is 'mount-svc serve' running?", None
        r = mpb.TargetReply()
        r.ParseFromString(out)
        return r.accepted, r.message, _msg_to_dict(r)

    def _do(self, op: str, args: dict):
        """Runs in a worker thread (Zenoh queries block)."""
        if op == "target":
            cmd = json_format.ParseDict(args, mpb.TargetCommand())
            return self._target(cmd)
        if op == "stop":
            # STOP goes straight to mount-rt (quick stop), then clears mount-svc's target.
            notes = []
            try:
                ok, msg = self.client.quick_stop()
                notes.append(f"mount-rt: {msg}")
            except MountError as e:
                ok = False
                notes.append(f"mount-rt: {e}")
            svc_ok, svc_msg, _ = self._target(mpb.TargetCommand(stop=mpb.Stop()))
            notes.append(f"mount-svc: {svc_msg}")
            return ok, "; ".join(notes), None
        if op == "hold":
            return self._target(mpb.TargetCommand(stop=mpb.Stop(keep_enabled=True)))
        if op == "park":
            return self._target(mpb.TargetCommand(goto_altaz=mpb.AltAz(az_deg=self.park[0], el_deg=self.park[1], name="park")))
        if op in ("enable", "disable"):
            axis = str(args.get("axis", ""))
            ok, msg = getattr(self.client, op)(axis)
            return ok, f"{op} {axis or 'all axes'}: {msg}", None
        if op == "fault_reset":
            ok, msg = self.client.fault_reset()
            return ok, f"fault reset: {msg}", None
        if op == "jog":
            jog = mpb.JogAxis(
                axis=str(args.get("axis", "")), by_deg=float(args.get("by_deg", 0)), to_deg=float(args.get("to_deg", 0)),
                absolute=bool(args.get("absolute", False)), vel_deg_s=float(args.get("vel_deg_s", 0)),
                acc_deg_s2=float(args.get("acc_deg_s2", 0)),
            )
            return self._target(mpb.TargetCommand(jog_axis=jog))
        if op == "tle_names":
            sats, note = self.tles.group(args.get("group", "stations"))
            return True, f"{len(sats)} objects ({note})", {"names": sorted(sats)}
        if op == "passes":
            return self._passes(args)
        if op == "camera":
            return self._camera_cmd(args)
        if op == "set_imaging":
            self.imaging.set(str(args.get("profile", "")))
            msg = self.imaging.message()
            self.loop.call_soon_threadsafe(self._broadcast, msg)  # every open GUI follows (_do runs in a worker thread)
            p = self.imaging.active
            return True, f"imaging profile: {p.name} ({p.arcsec_per_px:.2f}\"/px)", msg
        if op == "aircraft":
            return self._aircraft(args)
        if op == "conjunctions":
            return self._conjunctions(args)
        if op == "conjunction_detail":
            return self._conjunction_detail(args)
        if op == "pass_plan":
            return self._pass_plan(args)
        if op == "pass_preview":
            return self._pass_preview(args)
        return False, f"unknown op '{op}'", None

    def _passes(self, args: dict):
        """Upcoming passes, sorted by AOS.

        Watch list (default): the named objects, each pass planned by mount-svc.
        all=True: every object in the group. Passes are found here (cheap) and planned only on
        request (op pass_plan), because planning takes ~0.25 s per pass on the Pi. Groups with
        more than ALL_MAX objects are refused.
        min_max_el_deg: keep passes that culminate at least this high.
        start_ns: search from this time instead of now (tests; planning ahead).
        max_mag: keep passes predicted brighter than this (unknown magnitudes are dropped).
        min_size_arcsec: keep passes whose largest angular size (largest dimension over the
            closest range) is at least this (unknown sizes are dropped).
        lighting: "any" (default), "sunlit" (the satellite is sunlit for part of the pass) or
            "dark_sky" (sunlit, with the Sun below -6 deg at the site at culmination).
        Each row gets sunlit_frac (0..1) and sun_el_deg (at culmination).
        """
        group = args.get("group", "stations")
        hours = float(args.get("hours", 12))
        min_max_el = float(args.get("min_max_el_deg", 0))
        sats, note = self.tles.group(group)
        rows, missing, bad = [], [], 0
        if args.get("all"):
            if len(sats) > ALL_MAX:
                return False, (f"'{group}' has {len(sats)} objects, too many to search them all "
                               f"(limit {ALL_MAX}); list names in Watch instead"), None
            if self.settings.site is None:
                return False, "no site location", None
            mask = self.settings.tracking.horizon_min_el_deg * DEG
            t0 = int(args.get("start_ns") or 0) or time.time_ns()
            for name, (l1, l2) in sats.items():
                try:
                    sat = SatelliteTarget(self.settings.site, l1, l2, name=name)
                    found = planner.find_passes(sat, t0, t0 + int(hours * 3600 * NS), mask)
                except (ValueError, SgpError):
                    bad += 1  # decayed or a bad element set
                    continue
                for p in found:
                    if math.degrees(p.max_el) < min_max_el:
                        continue
                    rows.append({
                        "name": name, "line1": l1, "line2": l2, "planned": False,
                        "rise_ns": str(p.rise_ns), "culmination_ns": str(p.culmination_ns), "set_ns": str(p.set_ns),
                        "max_el_deg": math.degrees(p.max_el), "culmination_az_deg": math.degrees(p.culmination_az),
                        "chosen": {}, "plans": [],
                    })
        else:
            chosen, notes = _match_names(sats, args.get("names") or [])
            missing = notes["missing"]
            if notes["capped"]:
                return False, (f"'{notes['capped']}' matches {notes['count']} objects in '{group}'; use a more specific "
                               f"name or a catalog number (or Objects: all in group)"), {"passes": [], "names": sorted(sats)}
            for name in chosen:
                tle = sats[name]
                rep = self._svc_passes(name, tle, int(args.get("start_ns") or 0), hours)
                if rep is None:
                    return False, "no reply from mount-svc: is 'mount-svc serve' running?", None
                for p in rep.passes:
                    if p.max_el_deg < min_max_el:
                        continue
                    d = _msg_to_dict(p)
                    d.update(name=name, line1=tle[0], line2=tle[1], planned=True)
                    rows.append(d)
        lighting = args.get("lighting", "any")
        if lighting not in ("any", "sunlit", "dark_sky"):
            return False, f"unknown lighting filter '{lighting}'", None
        rows = [r for r in rows if self._add_lighting(r) and _lit_enough(r, lighting)]
        max_mag = args.get("max_mag")
        no_mag = 0
        if max_mag not in (None, "", "any"):
            no_mag = sum(r["peak_mag"] is None for r in rows)
            rows = [r for r in rows if r["peak_mag"] is not None and r["peak_mag"] <= float(max_mag)]
        min_size = args.get("min_size_arcsec")
        no_size = 0
        if min_size not in (None, "", "any", 0):
            no_size = sum(r["ang_size_arcsec"] is None for r in rows)
            rows = [r for r in rows if r["ang_size_arcsec"] is not None and r["ang_size_arcsec"] >= float(min_size)]
        rows = _merge_docked(rows)
        rows.sort(key=lambda r: int(r["rise_ns"]))
        shown = rows[:PASS_ROWS_MAX]
        what = f"{len(sats)} objects in '{group}'" if args.get("all") else (
            f"watching {', '.join(chosen)}" if chosen else "no watched objects")
        msg = f"{len(rows)} passes in {hours:g} h ({what}"
        msg += f", max el ≥ {min_max_el:g}°" if min_max_el > 0 else ""
        msg += {"any": ")", "sunlit": ", sunlit)", "dark_sky": ", sunlit in a dark sky)"}[lighting]
        if len(rows) > len(shown):
            msg += f"; showing the first {len(shown)}"
        msg += f"; TLEs {note}"
        if max_mag not in (None, "", "any"):
            msg += f"; brighter than mag {float(max_mag):g}" + (f" ({no_mag} without a magnitude hidden)" if no_mag else "")
        if min_size not in (None, "", "any", 0):
            msg += f"; at least {float(min_size):g}\" across" + (f" ({no_size} without a size hidden)" if no_size else "")
        msg += f"; {self.satcat.note}"
        if bad:
            msg += f"; {bad} element sets failed (decayed?)"
        if missing:
            msg += f"; nothing in '{group}' matches: {', '.join(missing)}"
        return True, msg, {"passes": shown, "names": sorted(sats)}

    def _add_lighting(self, row: dict) -> bool:
        """Adds what the passes table shows about seeing the satellite (always returns True):

        sunlit_frac     fraction of the pass with the satellite in sunlight (10 s steps)
        sun_el_deg      the Sun's elevation at the site at culmination
        peak_mag        brightest predicted visual magnitude while sunlit (None: unknown or
                        in shadow), and peak_mag_ns when; mag_estimated if from the RCS
        std_mag         the catalogue's intrinsic magnitude (quicksat.mag standard)
        size_m          dimensions (m); size_estimated if from the RCS
        ang_size_arcsec largest dimension over the closest range; min_range_km
        """
        keys = ("sunlit_frac", "sun_el_deg", "peak_mag", "peak_mag_ns", "mag_estimated", "std_mag", "size_m",
                "size_estimated", "ang_size_arcsec", "min_range_km")
        for k in keys:
            row.setdefault(k, None)
        if self.settings.site is None:
            return True
        site = self.settings.site
        rise, set_, culm = int(row["rise_ns"]), int(row["set_ns"]), int(row["culmination_ns"])
        sun_az, sun_el = astro.sun_observed(np.int64(culm), site)
        row["sun_el_deg"] = round(float(np.degrees(sun_el)), 1)
        try:
            sat = SatelliteTarget(site, row["line1"], row["line2"])
            t = np.linspace(rise, set_, max(2, (set_ - rise) // (10 * NS) + 1)).astype(np.int64)
            lit = sat.sunlit(t)
            enu = sat.geometric_enu_km(t)
        except (ValueError, SgpError):
            return True
        row["sunlit_frac"] = round(float(np.mean(lit)), 3)
        rng = np.linalg.norm(enu, axis=-1)
        row["min_range_km"] = round(float(rng.min()), 1)
        try:
            self.satcat.load()
            info = self.satcat.get(row["line1"][2:7])
        except Exception:  # noqa: BLE001 - brightness is a nicety; never fail the pass list
            log.exception("satellite catalogue")
            info = None
        if info is None:
            return True
        if info.size_m:
            row["size_m"], row["size_estimated"] = list(info.size_m), info.size_estimated
            row["ang_size_arcsec"] = round(math.degrees(info.max_size_m / 1000 / rng.min()) * 3600, 1)
        row["std_mag"], row["mag_estimated"] = info.std_mag, info.mag_estimated
        if info.std_mag is not None and lit.any():
            u = enu / rng[:, None]
            sun = astro.azel_to_enu(np.float64(sun_az), np.float64(sun_el))
            phase = np.arccos(np.clip(-(u @ sun), -1, 1))  # Sun-satellite-observer angle
            el = np.arcsin(np.clip(u[:, 2], -1, 1))
            mag = np.where(lit, satcat.predicted_mag(info.std_mag, rng, phase, el), np.inf)
            k = int(np.argmin(mag))
            row["peak_mag"], row["peak_mag_ns"] = round(float(mag[k]), 1), str(int(t[k]))
        return True

    # -- ADS-B aircraft --------------------------------------------------------------------------
    def _aircraft(self, args: dict):
        """Aircraft from the local ADS-B receiver within max_range_km (horizontal) of the site,
        extrapolated to now, with their closest approach over the next 15 min of straight
        flight. show: "up" (above the horizon mask now), "soon" (now or within 15 min) or "all".
        selected: an ICAO address; adds its trail and predicted path for the map and sky."""
        site = self.settings.site
        if site is None:
            return False, "no site location", None
        max_range = float(args.get("max_range_km", 100))
        show = args.get("show", "soon")
        mask = self.settings.tracking.horizon_min_el_deg
        now = time.time_ns()
        rows, sel_detail = [], None
        selected = str(args.get("selected") or "").lower()
        for icao, a in self.adsb.aircraft().items():
            tgt = adsb.AircraftTarget(site, a)
            enu = tgt.enu_m(np.array([now, now + NS], dtype=np.int64))
            horiz_km = float(np.hypot(enu[0, 0], enu[0, 1])) / 1000
            if horiz_km > max_range and icao != selected:
                continue
            rng = np.linalg.norm(enu, axis=-1)
            u = enu / rng[:, None]
            az, el = astro.enu_to_azel(enu[0])
            ca = adsb.closest_approach(tgt, now)
            el_deg, max_el = math.degrees(el), math.degrees(ca["max_el_rad"])
            if icao != selected and ((show == "up" and el_deg < mask) or (show == "soon" and max_el < mask)):
                continue
            lon, lat, _h = tgt.lonlat(np.int64(now))
            rows.append({
                "icao": icao, "label": a.label, "flight": a.flight, "reg": a.reg, "type": a.type, "squawk": a.squawk,
                "category": a.category, "alt_m": round(a.alt_m), "alt_source": a.alt_source, "gs_m_s": round(a.gs_m_s, 1),
                "track_deg": a.track_deg, "vrate_m_s": round(a.vrate_m_s, 1), "age_s": round((now - a.t_pos_ns) / NS, 1),
                "lon": round(float(lon[0]), 5), "lat": round(float(lat[0]), 5), "range_km": round(float(rng[0]) / 1000, 2),
                "horiz_km": round(horiz_km, 2), "az_deg": round(math.degrees(az), 2), "el_deg": round(el_deg, 2),
                "rate_deg_s": round(math.degrees(math.acos(min(1.0, float(u[0] @ u[1])))), 3),
                "cpa_in_s": round((ca["t_ns"] - now) / NS), "cpa_km": round(ca["range_m"] / 1000, 2),
                "cpa_el_deg": round(math.degrees(ca["el_rad"]), 1), "max_el_deg": round(max_el, 1),
                "max_rate_deg_s": round(math.degrees(ca["max_rate_rad_s"]), 3),
            })
            if icao == selected:
                t = np.arange(now, now + 600 * NS + 1, 5 * NS, dtype=np.int64)
                plon, plat, _ = tgt.lonlat(t)
                paz, pel = tgt.observed(t)
                sel_detail = {
                    "icao": icao,
                    "trail": [[round(lo, 5), round(la, 5)] for _t, la, lo, _alt in a.trail],
                    "path": [[round(float(x), 5), round(float(y), 5)] for x, y in zip(plon, plat)],
                    "sky": [[round(math.degrees(x), 2), round(math.degrees(y), 2), 1] for x, y in zip(paz, pel)],
                }
        rows.sort(key=lambda r: r["range_km"])
        what = {"up": "above the horizon mask", "soon": "up now or within 15 min", "all": "any elevation"}.get(show, show)
        msg = f"{len(rows)} aircraft within {max_range:g} km, {what}; {self.adsb.note}"
        return self.adsb.ok, msg, {"aircraft": rows, "site": [site.longitude_deg, site.latitude_deg],
                                   "selected": sel_detail, "mask_deg": mask}

    # -- SOCRATES conjunctions ------------------------------------------------------------------
    def _active_tles(self) -> dict[str, tuple[str, str, str]]:
        sats, _note = self.tles.group("active")
        return {satcat.norad_key(l1[2:7]): (name, l1, l2) for name, (l1, l2) in sats.items()}

    def _mag_at(self, cat: str, range_km: float, phase_rad: float, el_deg: float):
        info = self.satcat.get(cat)
        if info is None or info.std_mag is None:
            return None, False
        m = satcat.predicted_mag(info.std_mag, range_km, phase_rad, math.radians(el_deg))
        return round(float(m), 1), info.mag_estimated

    def _conjunctions(self, args: dict):
        """SOCRATES conjunctions in the next `hours` within `max_range_km`, with the event above
        the horizon mask at the site. lighting: any | sunlit | dark_sky (as for passes, at TCA).
        max_mag: keep events where either object is predicted at least this bright. Pairs moving
        slower than COORBIT_KM_S relative to each other (formation flyers, docked or co-located
        satellites) are left out unless coorbiting is true."""
        if self.settings.site is None:
            return False, "no site location", None
        hours = float(args.get("hours", 48))
        max_range = float(args.get("max_range_km", 1))
        lighting = args.get("lighting", "any")
        if lighting not in ("any", "sunlit", "dark_sky"):
            return False, f"unknown lighting filter '{lighting}'", None
        conj = self.socrates.load()
        self.satcat.load()
        now = int(args.get("start_ns") or 0) or time.time_ns()
        rows = socrates.screen(conj, self._active_tles(), self.settings.site, now, now + int(hours * 3600 * NS), max_range,
                               self.settings.tracking.horizon_min_el_deg)
        n_vis = len(rows)
        if not args.get("coorbiting"):
            rows = [r for r in rows if r["speed_km_s"] >= COORBIT_KM_S]
        n_co = n_vis - len(rows)
        if lighting != "any":
            rows = [r for r in rows if r["sunlit"] and (lighting == "sunlit" or r["sun_el_deg"] < -6)]
        for r in rows:
            phase = r.pop("phase_rad")
            for k in ("1", "2"):
                m, est = self._mag_at(r["cat" + k], r["obs_range_km"], phase, r["el_deg"]) if r["sunlit"] else (None, False)
                r["mag" + k], r["mag_estimated" + k] = m, est
            known = [m for m in (r["mag1"], r["mag2"]) if m is not None]
            r["mag"] = min(known) if known else None
            # Seen from the site the two are at most range / observer range apart at TCA.
            r["sep_max_arcsec"] = round(r["range_km"] / r["obs_range_km"] * 206264.8, 1)
        max_mag = args.get("max_mag")
        if max_mag not in (None, "", "any"):
            rows = [r for r in rows if r["mag"] is not None and r["mag"] <= float(max_mag)]
        rows.sort(key=lambda r: int(r["tca_ns"]))
        shown = rows[:PASS_ROWS_MAX]
        msg = f"{len(rows)} conjunctions within {max_range:g} km in {hours:g} h visible from the site"
        if n_co and not args.get("coorbiting"):
            msg += f" ({n_co} co-orbiting pairs hidden)"
        if len(rows) > len(shown):
            msg += f"; showing the first {len(shown)}"
        msg += f"; {self.socrates.note}; {self.satcat.note}"
        return True, msg, {"conjunctions": shown}

    def _conjunction_detail(self, args: dict):
        """Both objects' sky paths through the primary's pass, the closest approach as seen from
        the site, and the pass to track (the primary: the object with an element set)."""
        site = self.settings.site
        cat1, cat2, prim = str(args["cat1"]), str(args["cat2"]), str(args["primary"])
        tca = int(args["tca_ns"])
        try:
            pair = self.socrates.pair(cat1, cat2)
        except OSError as e:
            pair = {}
            log.warning("SOCRATES data.php: %s", e)
        active = self._active_tles()
        sec = cat2 if prim == cat1 else cat1
        p_tle = active.get(prim) or pair.get(prim)
        s_tle = pair.get(sec) or active.get(sec)
        if p_tle is None:
            return False, f"no element set for {prim}", None
        mask = self.settings.tracking.horizon_min_el_deg * DEG
        sat = SatelliteTarget(site, p_tle[1], p_tle[2], name=p_tle[0])
        found = planner.find_passes(sat, tca - 40 * 60 * NS, tca + 40 * 60 * NS, mask)
        p = next((q for q in found if q.rise_ns <= tca <= q.set_ns), None)
        if p is None:
            return False, f"{p_tle[0]} is below the horizon mask at TCA with the current elements", None
        row = {"name": p_tle[0], "line1": p_tle[1], "line2": p_tle[2], "planned": False, "chosen": {}, "plans": [],
               "rise_ns": str(p.rise_ns), "culmination_ns": str(p.culmination_ns), "set_ns": str(p.set_ns),
               "max_el_deg": math.degrees(p.max_el), "culmination_az_deg": math.degrees(p.culmination_az)}
        self._add_lighting(row)
        ok, _m, prev = self._pass_preview(row)
        if not ok:
            return False, _m, None
        az, el = sat.observed(np.int64(tca))
        lon, lat, _alt = self._subsat(sat, np.array([tca], dtype=np.int64))
        prev["ground"]["mark"] = [round(float(lon[0]), 2), round(float(lat[0]), 2)]
        out = {"pass": row, **prev, "tca_azel": [round(math.degrees(az), 2), round(math.degrees(el), 2)],
               "secondary": {"cat": sec, "name": s_tle[0] if s_tle else "", "has_elements": s_tle is not None},
               "sky2": None, "min_sep_arcsec": None, "min_sep_ns": None}
        if s_tle is not None:
            sat2 = SatelliteTarget(site, s_tle[1], s_tle[2], name=s_tle[0])
            ts = np.unique(np.concatenate([np.arange(p.rise_ns, p.set_ns, 5 * NS, dtype=np.int64), [p.set_ns]]))
            az2, el2 = sat2.observed(ts)
            lit2 = sat2.sunlit(ts)
            out["sky2"] = [[round(math.degrees(a), 2), round(math.degrees(e), 2), int(bool(s_))] for a, e, s_ in zip(az2, el2, lit2)]
            # Smallest angle between the two as seen from the site, within 2 min of TCA.
            tt = np.arange(tca - 120 * NS, tca + 120 * NS, NS // 10, dtype=np.int64)
            u1 = sat.geometric_enu_km(tt)
            u2 = sat2.geometric_enu_km(tt)
            u1 /= np.linalg.norm(u1, axis=-1, keepdims=True)
            u2 /= np.linalg.norm(u2, axis=-1, keepdims=True)
            sep = np.degrees(np.arccos(np.clip(np.sum(u1 * u2, axis=-1), -1, 1))) * 3600
            k = int(np.argmin(sep))
            out["min_sep_arcsec"], out["min_sep_ns"] = round(float(sep[k]), 1), str(int(tt[k]))
        plan_ok, _pm, plan = self._pass_plan(row)
        if plan_ok:
            row.update({k: plan["pass"][k] for k in ("chosen", "plans", "planned") if k in plan["pass"]})
        sep_txt = f", {out['min_sep_arcsec']:.0f}\" apart at best" if out["min_sep_arcsec"] is not None else ""
        return True, f"{p_tle[0]} × {out['secondary']['name'] or sec}{sep_txt}", out

    def _svc_passes(self, name: str, tle: tuple[str, str], start_ns: int, hours: float):
        req = mpb.PassesRequest(tle=mpb.Tle(name=name, line1=tle[0], line2=tle[1]), hours=hours, start_ns=start_ns)
        out = self.client.query("svc/passes", req.SerializeToString(), timeout=60)
        if out is None:
            return None
        rep = mpb.PassesReply()
        rep.ParseFromString(out)
        return rep

    def _pass_plan(self, args: dict):
        """mount-svc's plans for one pass (rows from an all-in-group search are unplanned)."""
        rise, set_ = int(args["rise_ns"]), int(args["set_ns"])
        start = rise - 120 * NS
        rep = self._svc_passes(str(args.get("name", "")), (str(args["line1"]), str(args["line2"])), start,
                               (set_ - start) / 3600 / NS + 0.05)
        if rep is None:
            return False, "no reply from mount-svc: is 'mount-svc serve' running?", None
        if not rep.ok or not rep.passes:
            return False, rep.message or "pass not found", None
        p = min(rep.passes, key=lambda q: abs(q.rise_ns - rise))
        d = _msg_to_dict(p)
        d.update(name=args.get("name", ""), line1=args["line1"], line2=args["line2"], planned=True)
        self._add_lighting(d)
        mode = d["chosen"]["mode"] or "no plan fits"
        return True, f"planned {d['name']}: {mode}", {"pass": d}

    # -- lifecycle ------------------------------------------------------------------------------
    async def _telemetry_pump(self) -> None:
        while True:
            await asyncio.sleep(0.1)  # 10 Hz to browsers
            if self._latest_tel is not None and self.clients:
                try:
                    self._broadcast(self._telemetry_msg(self._latest_tel))
                except Exception:  # noqa: BLE001
                    log.exception("telemetry conversion failed")

    async def _index(self, _request):
        return web.FileResponse(self.static_dir / "index.html")

    def make_app(self) -> web.Application:
        app = web.Application()

        async def no_stale_files(_request, response):
            # Browsers otherwise cache the GUI's modules heuristically and can mix old and new
            # versions after an update (an import fails and the page stays blank). no-cache
            # still allows cheap revalidation (ETag / 304).
            response.headers.setdefault("Cache-Control", "no-cache")

        app.on_response_prepare.append(no_stale_files)
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_static("/", self.static_dir, show_index=False)
        return app

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.client = MountClient(self.settings.zenoh)
        s = self.client.session
        self._subs = [
            s.declare_subscriber(self.client.key("mount/telemetry"), lambda smp: self._on_zenoh("telemetry", smp.payload.to_bytes())),
            s.declare_subscriber(self.client.key("svc/status"), lambda smp: self._on_zenoh("status", smp.payload.to_bytes())),
            s.declare_subscriber(self.client.key("camera/*/status"), lambda smp: self._on_zenoh("camstatus", smp.payload.to_bytes())),
            s.declare_subscriber(self.client.key("camera/*/frame"), lambda smp: self._on_zenoh("camframe", smp.payload.to_bytes())),
        ]
        self._pump = asyncio.ensure_future(self._telemetry_pump())
        self._runner = web.AppRunner(self.make_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("GUI on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        self._pump.cancel()
        for ws in list(self.clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()
        for sub in self._subs:
            try:
                sub.undeclare()
            except Exception:  # noqa: BLE001
                pass
        if self.client:
            self.client.close()
