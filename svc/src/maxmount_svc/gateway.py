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
      op: target (TargetCommand as JSON), stop, hold, park, passes, tle_names,
          pass_preview {name, line1, line2, rise_ns, culmination_ns, set_ns} (ground track + sky path),
          enable/disable {axis} and fault_reset (straight to mount-rt),
          jog {axis, by_deg | to_deg + absolute, vel_deg_s, acc_deg_s2} (through mount-svc)
"""
from __future__ import annotations

import asyncio
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

from . import astro, kinematics
from .config import SystemSettings
from .mount import MountClient, MountError
from .proto import mpb
from .targets import SatelliteTarget

log = logging.getLogger(__name__)
DEG = math.pi / 180
NS = 1_000_000_000
REPO = Path(__file__).resolve().parents[3]
STATIC_DIR = REPO / "gui"
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
TLE_GROUPS = ("stations", "visual", "active", "starlink", "weather", "science")
TLE_MAX_AGE_S = 6 * 3600


def _msg_to_dict(m) -> dict:
    return json_format.MessageToDict(m, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)


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
                if not path.exists():
                    raise RuntimeError(f"cannot fetch TLE group '{group}': {e}") from None
                log.warning("TLE download failed (%s); using the cached copy", e)
        lines = [ln.rstrip() for ln in path.read_text().splitlines() if ln.strip()]
        sats: dict[str, tuple[str, str]] = {}
        for i in range(len(lines) - 2):
            if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 ") and not lines[i].startswith(("1 ", "2 ")):
                sats[lines[i].strip()] = (lines[i + 1], lines[i + 2])
        age_h = (time.time() - path.stat().st_mtime) / 3600
        return sats, f"{note}, {age_h:.1f} h old"


class Gateway:
    def __init__(self, settings: SystemSettings, host: str = "0.0.0.0", port: int = 8080, static_dir: Path = STATIC_DIR,
                 park_az_deg: float = 0.0, park_el_deg: float = 45.0):
        self.settings = settings
        self.host, self.port = host, port
        self.static_dir = Path(static_dir)
        self.park = (park_az_deg, park_el_deg)
        self.tles = TleCache()
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

    # -- Zenoh side ----------------------------------------------------------------------------
    def _on_zenoh(self, kind: str, payload: bytes) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._store, kind, payload)

    def _store(self, kind: str, payload: bytes) -> None:
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
        if op == "pass_preview":
            return self._pass_preview(args)
        return False, f"unknown op '{op}'", None

    def _passes(self, args: dict):
        group = args.get("group", "stations")
        names = args.get("names") or []
        hours = float(args.get("hours", 12))
        sats, note = self.tles.group(group)
        rows, missing = [], []
        for name in names:
            tle = sats.get(name)
            if tle is None:
                missing.append(name)
                continue
            req = mpb.PassesRequest(tle=mpb.Tle(name=name, line1=tle[0], line2=tle[1]), hours=hours)
            out = self.client.query("svc/passes", req.SerializeToString(), timeout=60)
            if out is None:
                return False, "no reply from mount-svc: is 'mount-svc serve' running?", None
            rep = mpb.PassesReply()
            rep.ParseFromString(out)
            for i, p in enumerate(rep.passes):
                d = _msg_to_dict(p)
                d.update(name=name, pass_index=i, line1=tle[0], line2=tle[1])
                rows.append(d)
        rows.sort(key=lambda r: int(r["rise_ns"]))
        msg = f"{len(rows)} passes in {hours:g} h; TLEs {note}"
        if missing:
            msg += f"; not in '{group}': {', '.join(missing)}"
        return True, msg, {"passes": rows}

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
