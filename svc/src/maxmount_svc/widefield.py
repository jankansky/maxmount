# SPDX-License-Identifier: GPL-3.0-or-later
"""The widefield (finder) camera service, `mount-svc widefield` (camera id "widefield").

A GigE camera with its own lens, like a finder scope, fixed to the tube: live view at a few
frames per second, single frames saved as FITS on the Pi, and plate solving (platesolve.py):
where a frame really points on the sky, compared with where the mount believed it pointed.
Every solve is logged as a candidate pointing-model point (<data_dir>/widefield/solves.jsonl);
pointing model runs (pointrun.py, in mount-svc) use the same solve command.

A solve takes the first frame whose exposure starts after the request, and the mount's
encoder angles at its mid-exposure time, interpolated from mount telemetry this service keeps
for the last minutes. The hint for the solver is where the mount believes it points: the
encoders through the active pointing model (pointfit.load_model), as RA/Dec.

Backends:
  AravisBackend   a GigE Vision camera (Allied Vision GS1380 and others) through Aravis 0.8
                  (PyGObject: gi.repository.Aravis; apt install gir1.2-aravis-0.8 python3-gi).
                  Mono8/Mono16, exposure, gain. Imported only when used.
  WidefieldSimBackend  renders catalogue stars (skycat, mag <= 8) where the camera REALLY
                  points: the encoder angles through the inverse of a "true" pointing model
                  (cameras.widefield.sim_model) and a sensor rotation, with sky background,
                  photon and read noise, so plate solves and pointing runs have real errors
                  to find.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path

import erfa
import numpy as np

from . import astro, kinematics, platesolve, pointfit, skycat
from .camera import CameraError, CameraService, Shot
from .closedloop import CameraGeometry
from .config import SystemSettings, WidefieldSettings
from .pointing import PointingModel
from .proto import mpb
from .zwo import fits_bytes

log = logging.getLogger(__name__)
NS = 1_000_000_000
DEG = math.pi / 180
ARCSEC = DEG / 3600
FORMATS = ["MONO16", "MONO8"]


# -- simulated sky ------------------------------------------------------------------------------
def boresight_frame(a: float, e: float):
    """Unit vectors (ENU) of a mechanical pose: boresight b, +azimuth-axis motion h and
    +elevation-axis motion g (as simsky.project)."""
    b = np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])
    h = np.array([math.cos(a), -math.sin(a), 0.0])
    g = np.array([-math.sin(a) * math.sin(e), -math.cos(a) * math.sin(e), math.cos(e)])
    return b, h, g


def icrs_of(site, t_ns: int, az: float, el: float) -> tuple[float, float]:
    """Observed (refracted) az/el -> ICRS RA/Dec (rad)."""
    u1, u2 = astro.utc_jd(np.int64(t_ns))
    elong, phi, hm, xp, yp, phpa, tc, rh, wl = astro._site_args(site)
    ra, dec = erfa.atoc13("A", az, math.pi / 2 - el, u1, u2, site.dut1_s, elong, phi, hm, xp, yp, phpa, tc, rh, wl)
    return float(ra), float(dec)


class ObservedFrame:
    """Plate solving in observed (refracted) azimuth/elevation at one instant: the frame the
    camera really sees, so refraction is in the catalogue positions instead of bending the
    field (platesolve.solve's frame)."""

    def __init__(self, site, t_ns: int, cat: skycat.Stars | None = None):
        self.site, self.t_ns = site, np.int64(t_ns)
        self.cat = cat or skycat.stars()

    def stars(self, idx):
        c = self.cat
        pmr = c.pmra[idx] / np.maximum(np.cos(c.dec[idx]), 1e-6)  # ERFA: dRA/dt
        return astro.observed_from_icrs(self.t_ns, self.site, c.ra[idx], c.dec[idx], pmr, c.pmdec[idx])

    def from_icrs(self, ra, dec):
        return astro.observed_from_icrs(self.t_ns, self.site, ra, dec)

    def to_icrs(self, az, el):
        az, el = np.atleast_1d(np.asarray(az, float)), np.atleast_1d(np.asarray(el, float))
        out = np.array([icrs_of(self.site, int(self.t_ns), float(a), float(e)) for a, e in zip(az, el)])
        return (out[0, 0], out[0, 1]) if out.shape[0] == 1 else (out[:, 0], out[:, 1])


def render_sky(cfg: WidefieldSettings, site, t_ns: int, a: float, e: float, exposure_s: float, gain_db: float,
               rng: np.random.Generator, rotation_deg: float | None = None, flip: bool | None = None,
               cloudy: bool = False, mag_limit: float = 8.0) -> np.ndarray:
    """A 12-bit frame (uint16 ADU, not shifted) of the sky seen by a camera whose boresight is
    the ideal mechanical pose (a, e), at t_ns. Sensor +x at rotation_deg from +azimuth."""
    w, h = cfg.width, cfg.height
    rot = cfg.sim_rotation_deg if rotation_deg is None else rotation_deg
    geom = CameraGeometry.from_config(cfg.arcsec_per_px, rot, cfg.sim_flip if flip is None else flip)
    e_img = np.full((h, w), 25.0 * exposure_s, np.float32)  # sky background, e-/px
    if not cloudy and site is not None:
        az, el = (float(v) for v in kinematics.from_mech(a, e))
        rc, dc = icrs_of(site, t_ns, az, el)
        cat = skycat.stars()
        radius = 0.5 * math.hypot(w, h) * cfg.arcsec_per_px * ARCSEC + 0.5 * DEG
        idx = cat.cone(rc, dc, radius, mag_max=mag_limit)
        if len(idx):
            u1, u2 = astro.utc_jd(np.int64(t_ns))
            elong, phi, hm, xp, yp, phpa, tc, rh, wl = astro._site_args(site)
            pmr = cat.pmra[idx] / np.maximum(np.cos(cat.dec[idx]), 1e-6)
            aob, zob, *_ = erfa.atco13(cat.ra[idx], cat.dec[idx], pmr, cat.pmdec[idx], 0.0, 0.0, u1, u2, site.dut1_s,
                                       elong, phi, hm, xp, yp, phpa, tc, rh, wl)
            u = astro.azel_to_enu(aob, math.pi / 2 - zob)
            b, hh, gg = boresight_frame(a, e)
            ub = u @ b
            ok = ub > math.cos(radius)
            x, y = (u[ok] @ hh) / ub[ok], (u[ok] @ gg) / ub[ok]
            px = np.linalg.solve(geom.m, np.stack([x, y]))  # (2, N) sensor px from the centre
            k1 = cfg.sim_distortion_k1
            if k1:  # the lens' radial distortion (platesolve's convention), inverted by iteration
                r2_of = lambda q: (q ** 2).sum(axis=0) / (0.5 * math.hypot(w, h)) ** 2  # noqa: E731
                pu, px = px, px.copy()
                for _ in range(6):
                    px = pu / (1 + k1 * r2_of(px))
            cx, cy = px[0] + (w - 1) / 2, px[1] + (h - 1) / 2
            flux = 10 ** (-0.4 * (cat.mag[idx][ok] - 17.0)) * exposure_s  # e-: mag 8 ~ 4000 e-/s
            sigma, r = 1.3, 6
            for x0, y0, f in zip(cx, cy, flux):
                if not (-r < x0 < w + r and -r < y0 < h + r):
                    continue
                xi, yi = int(round(x0)), int(round(y0))
                xs, ys = slice(max(xi - r, 0), min(xi + r + 1, w)), slice(max(yi - r, 0), min(yi + r + 1, h))
                gx = np.arange(xs.start, xs.stop) - x0
                gy = np.arange(ys.start, ys.stop) - y0
                if not len(gx) or not len(gy):
                    continue
                e_img[ys, xs] += (f / (2 * math.pi * sigma ** 2)) * np.exp(-(gy[:, None] ** 2 + gx[None, :] ** 2) / (2 * sigma ** 2))
    noise = rng.standard_normal(e_img.shape, dtype=np.float32)
    noise *= np.sqrt(e_img + 3.5 ** 2)
    e_img += noise
    adu = e_img * (0.25 * 10 ** (gain_db / 20)) + 200
    return np.clip(adu, 0, 4095).astype(np.uint16)


# -- backends -----------------------------------------------------------------------------------
class _WidefieldBase:
    storage = ("pi",)
    bulb_supported = False
    records_video = False
    simulated = False

    def __init__(self, cfg: WidefieldSettings):
        self.cfg = cfg
        self.model = "widefield"
        self.sensor = (cfg.width, cfg.height)
        self.pixel_um = cfg.pixel_um
        self.limits = {"exposure_ms": (0.1, 30000.0, 0.1), "gain": (0.0, 48.0, 0.1)}
        self.v = {"exposure_ms": float(cfg.exposure_ms), "gain": float(cfg.gain), "format": "MONO16"}
        self._lock = threading.Lock()
        self._cam = threading.RLock()  # the camera itself: video thread or a single exposure
        self._video = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest: np.ndarray | None = None
        self._latest_t = 0      # mid-exposure, ns
        self._latest_exp = 0.0
        self._seq = 0
        self._fps = 0.0
        self._dropped = 0
        self._temperature = float("nan")

    # -- settings -------------------------------------------------------------------------------
    def settings(self) -> list[dict]:
        def num(name, label, unit):
            lo, hi, step = self.limits[name]
            return {"name": name, "label": label, "value": f"{self.v[name]:g}", "numeric": True, "min": lo, "max": hi,
                    "step": step, "unit": unit}
        return [num("exposure_ms", "Exposure", "ms"), num("gain", "Gain", "dB"),
                {"name": "format", "label": "Format", "value": self.v["format"], "choices": FORMATS}]

    def set(self, name: str, value: str) -> None:
        if name in self.limits:
            try:
                x = float(value)
            except ValueError:
                raise CameraError(f"{name}: '{value}' is not a number") from None
            lo, hi, _ = self.limits[name]
            if not lo <= x <= hi:
                raise CameraError(f"{name}: {x:g} outside {lo:g}..{hi:g}")
            new = x
        elif name == "format" and value in FORMATS:
            new = value
        else:
            raise CameraError(f"{name}: '{value}' not offered")
        was = self._video and name == "format"
        if was:
            self._stop_video()
        self.v[name] = new
        with self._cam:
            self._apply(name)
        if was:
            self._start_video()

    def battery(self) -> int:
        return -1

    def exposure_s(self) -> float:
        return self.v["exposure_ms"] / 1000

    def full_scale(self) -> float:
        return 65535.0 if self.v["format"] == "MONO16" else 255.0

    def extra_status(self) -> dict:
        w, h = self.sensor
        return {"fps": self._fps if self._video else 0.0, "dropped": self._dropped, "temperature_c": self._temperature,
                "roi": [0, 0, w, h], "sensor_width": w, "sensor_height": h}

    def _out(self, adu12: np.ndarray) -> np.ndarray:
        """12-bit ADU -> the frame in the current format (16-bit: left-aligned, like the camera)."""
        return (adu12 << 4).astype(np.uint16) if self.v["format"] == "MONO16" else (adu12 >> 4).astype(np.uint8)

    # -- video --------------------------------------------------------------------------------
    def set_liveview(self, on: bool) -> None:
        if on and not self._video:
            self._start_video()
        elif not on and self._video:
            self._stop_video()

    def _start_video(self) -> None:
        self._stop.clear()
        with self._lock:
            self._latest, self._seq, self._fps = None, 0, 0.0
        with self._cam:
            self._begin_video()
        self._video = True
        self._thread = threading.Thread(target=self._video_loop, daemon=True, name="widefield-video")
        self._thread.start()

    def _stop_video(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._video = False
        with self._cam:
            self._end_video()

    def _video_loop(self) -> None:
        t_last, n = time.monotonic(), 0
        while not self._stop.is_set():
            try:
                with self._cam:
                    frame, t_mid = self._grab()
            except Exception:  # noqa: BLE001 - a timeout; the next frame may come
                if self._stop.is_set():
                    break
                self._dropped += 1
                time.sleep(0.05)
                continue
            with self._lock:
                self._latest, self._latest_t, self._latest_exp = frame, t_mid, self.exposure_s()
                self._seq += 1
            n += 1
            now = time.monotonic()
            if now - t_last >= 2.0:
                self._fps, t_last, n = n / (now - t_last), now, 0

    def preview_array(self):
        if not self._video:
            raise CameraError("live view is off")
        end = time.monotonic() + max(3.0, 2 * self.exposure_s() + 2)
        while time.monotonic() < end:
            with self._lock:
                if self._latest is not None:
                    return self._latest.copy(), self.full_scale()
            time.sleep(0.01)
        raise CameraError("no frame from the camera")

    def frame_after(self, t_ns: int, timeout_s: float | None = None) -> tuple[np.ndarray, int, float]:
        """The first frame whose exposure starts at or after t_ns: (frame, mid-exposure ns, exposure s)."""
        timeout_s = timeout_s if timeout_s is not None else 3 * self.exposure_s() + 8
        if not self._video:
            with self._cam:
                frame, t_mid = self._snapshot()
            return frame, t_mid, self.exposure_s()
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            with self._lock:
                if self._latest is not None and self._latest_t - int(self._latest_exp * NS / 2) >= t_ns:
                    return self._latest.copy(), self._latest_t, self._latest_exp
            time.sleep(0.01)
        raise CameraError("no new frame from the camera")

    def capture(self, bulb_s: float, abort: threading.Event, download_raw: bool) -> Shot:
        frame, t_mid, exp_s = self.frame_after(time.time_ns())
        t_start = t_mid - int(exp_s * NS / 2)
        hdr = {
            "DATE-OBS": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start / NS)) + f".{t_start % NS // 1_000_000:03d}",
            "EXPTIME": exp_s, "GAIN": float(self.v["gain"]), "XPIXSZ": self.pixel_um, "YPIXSZ": self.pixel_um,
            "FOCALLEN": self.cfg.focal_mm, "INSTRUME": self.model, "IMAGETYP": "Light Frame", "TIMESYS": "UTC",
            "TIMENOTE": "approximate: frame arrival minus exposure",
        }
        w, h = self.sensor
        name = f"{self.v['format']}_{w}x{h}_{self.v['exposure_ms']:g}ms.fits"
        bits = 16 if frame.dtype == np.uint16 else 8
        return Shot(files=[(name, fits_bytes(frame, hdr))], preview_array=frame, full_scale=self.full_scale(),
                    gain=float(self.v["gain"]), bit_depth=bits, note="widefield")

    # Implemented by the backends.
    def _apply(self, name: str) -> None: ...
    def _begin_video(self) -> None: ...
    def _end_video(self) -> None: ...
    def _grab(self) -> tuple[np.ndarray, int]: ...
    def _snapshot(self) -> tuple[np.ndarray, int]: ...
    def open(self) -> str: ...
    def close(self) -> None: ...


class WidefieldSimBackend(_WidefieldBase):
    """The sky as the widefield camera would see it. pointing(t_ns) -> encoder (A, E) in rad,
    or None (no telemetry: an empty sky)."""

    simulated = True

    def __init__(self, cfg: WidefieldSettings, site, pointing=None, seed: int = 11, time_scale: float = 1.0):
        super().__init__(cfg)
        self.model = "GigE widefield (simulated)"
        self.site = site
        self.pointing = pointing
        self.truth = PointingModel(dict(cfg.sim_model))
        self.time_scale = time_scale  # tests: exposures take this fraction of their time
        self._rng = np.random.default_rng(seed)
        self._temperature = 18.0

    def open(self) -> str:
        return self.model

    def close(self) -> None:
        if self._video:
            self._stop_video()

    def true_pose(self, t_ns: int):
        """The ideal pose the camera really looks along (rad), or None."""
        p = self.pointing(t_ns) if self.pointing else None
        if p is None:
            return None
        a, e = pointfit.ideal_from_commanded(self.truth, p[0], p[1])
        return float(a), float(e)

    def _render(self) -> tuple[np.ndarray, int]:
        exp = self.exposure_s()
        t0 = time.time_ns()
        if self._stop.wait(min(max(exp, 0.2), 30) * self.time_scale):
            raise CameraError("stopped")
        t_mid = t0 + int(exp * NS / 2) if self.time_scale >= 1 else (t0 + time.time_ns()) // 2
        pose = self.true_pose(t_mid)
        if pose is None:
            adu = render_sky(self.cfg, None, t_mid, 0.0, 0.5, exp, self.v["gain"], self._rng, cloudy=True)
        else:
            adu = render_sky(self.cfg, self.site, t_mid, pose[0], pose[1], exp, self.v["gain"], self._rng,
                             cloudy=self.cfg.sim_cloudy)
        return self._out(adu), t_mid

    def _grab(self):
        return self._render()

    def _snapshot(self):
        self._stop.clear()
        return self._render()


class AravisBackend(_WidefieldBase):
    """A GigE Vision camera through Aravis 0.8 (PyGObject). Untested without the camera: kept
    small and close to Aravis' documented API (arv_camera_*)."""

    def __init__(self, cfg: WidefieldSettings):
        super().__init__(cfg)
        try:
            import gi

            gi.require_version("Aravis", "0.8")
            from gi.repository import Aravis  # type: ignore[import-not-found]
        except (ImportError, ValueError) as e:
            raise CameraError(f"Aravis not available ({e}): apt install gir1.2-aravis-0.8 python3-gi "
                              "and give the venv system site packages (see svc/README.md)") from None
        self.Aravis = Aravis
        self.cam = None
        self.stream = None
        self._payload = 0
        self._bits = 16

    def full_scale(self) -> float:
        return float((1 << self._bits) - 1)

    def open(self) -> str:
        A = self.Aravis
        try:
            self.cam = A.Camera.new(self.cfg.camera or None)
        except Exception as e:  # noqa: BLE001 - GLib.Error
            raise CameraError(f"no GigE camera{f' {self.cfg.camera}' if self.cfg.camera else ''}: {e}") from None
        cam = self.cam
        self.model = f"{cam.get_vendor_name()} {cam.get_model_name()}".strip()
        _x, _y, w, h = cam.get_region()
        sw, sh = cam.get_sensor_size()
        if (w, h) != (sw, sh):
            cam.set_region(0, 0, sw, sh)
        self.sensor = (sw, sh)
        if (sw, sh) != (self.cfg.width, self.cfg.height):
            log.warning("widefield: sensor %dx%d differs from cameras.widefield width/height %dx%d (field of view uses the config)",
                        sw, sh, self.cfg.width, self.cfg.height)
        lo, hi = cam.get_exposure_time_bounds()
        self.limits["exposure_ms"] = (lo / 1000, min(hi / 1000, 30000.0), 0.01)
        if cam.is_gain_available():
            glo, ghi = cam.get_gain_bounds()
            self.limits["gain"] = (glo, ghi, 0.1)
        if cam.is_gv_device():
            if self.cfg.packet_size:
                cam.gv_set_packet_size(int(self.cfg.packet_size))
            else:
                cam.gv_auto_packet_size()
        for n in ("format", "exposure_ms", "gain"):
            self._apply(n)
        return self.model

    def close(self) -> None:
        if self._video:
            self._stop_video()
        self.cam = None

    def _apply(self, name: str) -> None:
        if self.cam is None:
            return
        A = self.Aravis
        if name == "exposure_ms":
            self.cam.set_exposure_time(self.v["exposure_ms"] * 1000)
        elif name == "gain" and self.cam.is_gain_available():
            self.cam.set_gain(self.v["gain"])
        elif name == "format":
            # Deep frames: Mono16 if the camera has it, else Mono12 in 16-bit words (the
            # Allied Vision GS1380 offers Mono8 / Mono12 / Mono12Packed, not Mono16).
            self._bits = 8
            if self.v["format"] == "MONO16":
                try:
                    offered = set(self.cam.dup_available_pixel_formats_as_strings() or [])
                except Exception:  # noqa: BLE001 - GLib.Error: try Mono16 anyway
                    offered = {"Mono16"}
                for fmt, name_, bits in ((A.PIXEL_FORMAT_MONO_16, "Mono16", 16), (A.PIXEL_FORMAT_MONO_12, "Mono12", 12)):
                    if name_ in offered:
                        self.cam.set_pixel_format(fmt)
                        self._bits = bits
                        return
                log.warning("widefield: neither Mono16 nor Mono12 offered (%s); using Mono8", sorted(offered))
            self.cam.set_pixel_format(A.PIXEL_FORMAT_MONO_8)

    def _begin_video(self) -> None:
        A = self.Aravis
        self.cam.set_acquisition_mode(A.AcquisitionMode.CONTINUOUS)
        self.stream = self.cam.create_stream(None, None)
        self._payload = self.cam.get_payload()
        for _ in range(6):
            self.stream.push_buffer(A.Buffer.new_allocate(self._payload))
        self.cam.start_acquisition()

    def _end_video(self) -> None:
        if self.cam is not None:
            try:
                self.cam.stop_acquisition()
            except Exception:  # noqa: BLE001
                pass
        self.stream = None

    def _pop(self, timeout_s: float) -> tuple[np.ndarray, int]:
        A = self.Aravis
        buf = self.stream.timeout_pop_buffer(int(timeout_s * 1e6))
        if buf is None:
            raise CameraError("no frame from the camera (timeout)")
        try:
            if buf.get_status() != A.BufferStatus.SUCCESS:
                self._dropped += 1
                raise CameraError(f"frame lost ({buf.get_status().value_nick})")
            w, h = buf.get_image_width(), buf.get_image_height()
            dtype = np.uint16 if self._bits > 8 else np.uint8
            a = np.frombuffer(buf.get_data(), dtype=dtype)[: w * h].reshape(h, w).copy()
            # Host time when the frame arrived (ns since the epoch), minus half the exposure and
            # the camera's readout + transfer (cameras.widefield.latency_ms, to be measured).
            t_arrive = int(buf.get_system_timestamp()) or time.time_ns()
        finally:
            self.stream.push_buffer(buf)
        return a, t_arrive - int(self.exposure_s() * NS / 2) - int(self.cfg.latency_ms * 1e6)

    def _grab(self):
        return self._pop(self.exposure_s() + 3.0)

    def _snapshot(self):
        self._begin_video()
        try:
            return self._pop(self.exposure_s() + 5.0)
        finally:
            self._end_video()


# -- the service ----------------------------------------------------------------------------------
class TelemetryHistory:
    """Mount axis angles over the last minutes, from mount/telemetry, for frame times."""

    def __init__(self, seconds: float = 300.0):
        self._q: deque = deque()
        self._lock = threading.Lock()
        self.seconds = seconds

    def add(self, m) -> None:
        ax = {a.axis: a for a in m.axes}
        if "az" not in ax or "el" not in ax:
            return
        row = (int(m.t_ns), ax["az"].act_position_rad, ax["el"].act_position_rad, ax["az"].cmd_position_rad,
               ax["el"].cmd_position_rad, ax["az"].cmd_velocity_rad_s, ax["el"].cmd_velocity_rad_s)
        with self._lock:
            self._q.append(row)
            while self._q and self._q[0][0] < row[0] - self.seconds * NS:
                self._q.popleft()

    def at(self, t_ns: int, max_gap_s: float = 2.0) -> dict | None:
        """Axis angles (rad) at t_ns: interpolated, or extrapolated up to max_gap_s past the newest sample."""
        with self._lock:
            q = list(self._q) if self._q and (self._q[-1][0] >= t_ns - max_gap_s * NS) else None
        if not q or t_ns < q[0][0]:
            return None
        ts = np.array([r[0] for r in q], dtype=np.int64)
        k = int(np.searchsorted(ts, t_ns))
        if k >= len(q):
            r = q[-1]
            dt = (t_ns - r[0]) / NS
            return {"act_a": r[1] + r[5] * dt, "act_e": r[2] + r[6] * dt, "cmd_a": r[3] + r[5] * dt, "cmd_e": r[4] + r[6] * dt}
        r0, r1 = q[max(k - 1, 0)], q[k]
        f = 0.0 if r1[0] == r0[0] else (t_ns - r0[0]) / (r1[0] - r0[0])
        lerp = lambda i: r0[i] + (r1[i] - r0[i]) * f  # noqa: E731
        return {"act_a": lerp(1), "act_e": lerp(2), "cmd_a": lerp(3), "cmd_e": lerp(4)}


class WidefieldService(CameraService):
    def __init__(self, client, backend, settings: SystemSettings):
        cfg = settings.widefield
        super().__init__(client, backend, "widefield", cfg.data_dir, cfg.liveview_fps)
        self.settings = settings
        self.cfg = cfg
        self.history = TelemetryHistory()
        self._solution: mpb.PlateSolution | None = None
        self._solving = False
        self._pmodel = PointingModel()
        self._pmodel_key = None

    def start(self) -> None:
        super().start()
        self._decls.append(self.client.session.declare_subscriber(self.client.key("mount/telemetry"), self._on_telemetry))

    def _on_telemetry(self, sample) -> None:
        m = mpb.MountTelemetry()
        try:
            m.ParseFromString(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        self.history.add(m)

    def pointing(self, t_ns: int):
        """Encoder (A, E) at t_ns for the simulator, or None."""
        p = self.history.at(t_ns)
        return None if p is None else (p["act_a"], p["act_e"])

    def model(self) -> PointingModel:
        """The active pointing model (reloaded when its file changes: a run was applied)."""
        p = pointfit.model_path(self.settings)
        try:
            key = (str(p), p.stat().st_mtime)
        except OSError:
            key = (str(p), None)
        if key != self._pmodel_key:
            self._pmodel_key = key
            self._pmodel = pointfit.load_model(self.settings)
        return self._pmodel

    # -- commands -----------------------------------------------------------------------------
    def handle(self, cmd) -> tuple[bool, str]:
        if cmd.WhichOneof("command") != "solve":
            return super().handle(cmd)
        with self._lock:
            connected, solving = self._connected, self._solving
            self._solving = connected and not solving
        if not connected:
            return False, f"camera not connected ({self._message})"
        if solving:
            return False, "already solving"
        arg = mpb.Solve()
        arg.CopyFrom(cmd.solve)
        threading.Thread(target=self._solve_thread, args=(arg, time.time_ns()), daemon=True, name="solve").start()
        return True, "solving the next frame"

    def _solve_thread(self, arg, t_req: int) -> None:
        self._set(message="solving: waiting for a frame")
        try:
            sol = self.solve(arg, t_req)
        except Exception as e:  # noqa: BLE001 - always report
            log.exception("solve failed")
            sol = mpb.PlateSolution(t_ns=time.time_ns(), ok=False, message=f"{type(e).__name__}: {e}", id=arg.id)
        with self._lock:
            self._solution, self._solving = sol, False
        self._set(message=sol.message)

    def solve(self, arg, t_req: int) -> mpb.PlateSolution:
        site = self.settings.site
        cfg = self.cfg
        frame, t_mid, exp_s = self.backend.frame_after(t_req)
        sol = mpb.PlateSolution(t_ns=t_mid, id=arg.id, exposure_s=exp_s, solver=cfg.solver)
        if site is not None:
            sol.temperature_c, sol.pressure_hpa = site.temperature_c, site.pressure_hpa
        mount = self.history.at(t_mid)
        if mount is None or site is None:
            sol.ok, sol.message = False, "no solve: no mount telemetry at the frame's time (is mount-rt running?)" if site else "no site"
            return sol
        sol.has_mount = True
        sol.enc_az_deg, sol.enc_el_deg = math.degrees(mount["act_a"]), math.degrees(mount["act_e"])
        sol.cmd_az_deg, sol.cmd_el_deg = math.degrees(mount["cmd_a"]), math.degrees(mount["cmd_e"])
        a_i, e_i = pointfit.ideal_from_commanded(self.model(), mount["act_a"], mount["act_e"])
        baz, bel = (float(v) for v in kinematics.from_mech(a_i, e_i))
        bra, bdec = icrs_of(site, t_mid, baz, bel)
        sol.mount_az_deg, sol.mount_el_deg = math.degrees(baz), math.degrees(bel)
        sol.mount_ra_deg, sol.mount_dec_deg = math.degrees(bra), math.degrees(bdec)
        radius = (arg.search_radius_deg or cfg.search_radius_deg) * DEG
        adu = frame.astype(np.float32)
        if cfg.solver == "astap":
            hdr = {"EXPTIME": exp_s, "XPIXSZ": cfg.pixel_um, "YPIXSZ": cfg.pixel_um, "FOCALLEN": cfg.focal_mm}
            r = platesolve.solve_astap(fits_bytes(frame, hdr), frame.shape[1], frame.shape[0], bra, bdec, cfg.fov_deg[1],
                                       math.degrees(radius), cfg.astap_path)
        else:
            r = platesolve.solve(adu, bra, bdec, cfg.arcsec_per_px * ARCSEC, search_radius=radius,
                                 frame=ObservedFrame(site, t_mid), k1=cfg.distortion_k1)
        sol.ok, sol.message, sol.solve_s = r.ok, r.message, r.solve_s
        sol.stars, sol.matched = r.stars, r.matched
        if not r.ok:
            return sol
        sol.ra_deg, sol.dec_deg, sol.roll_deg = math.degrees(r.ra), math.degrees(r.dec), r.roll_deg
        sol.arcsec_per_px, sol.mirrored = r.arcsec_per_px, r.mirrored
        sol.rms_arcsec = r.rms_arcsec if math.isfinite(r.rms_arcsec) else 0.0
        az, el = astro.observed_from_icrs(np.int64(t_mid), site, r.ra, r.dec)
        az, el = float(az), float(el)
        sol.az_deg, sol.el_deg = math.degrees(az), math.degrees(el)
        daz = (az - baz + math.pi) % (2 * math.pi) - math.pi
        sol.err_xel_arcsec = daz * math.cos(el) / ARCSEC
        sol.err_el_arcsec = (el - bel) / ARCSEC
        sol.message = (f"{r.message}; pointing error {sol.err_xel_arcsec:+.0f}\" x-el, {sol.err_el_arcsec:+.0f}\" el")
        self._log(sol)
        return sol

    def _log(self, sol) -> None:
        """Every solve is a candidate pointing point (design: keep learning)."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            from google.protobuf import json_format

            row = json_format.MessageToDict(sol, preserving_proto_field_name=True)
            with open(self.data_dir / "solves.jsonl", "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            log.warning("widefield: solve log: %s", e)

    # -- status -------------------------------------------------------------------------------
    def status(self) -> mpb.CameraStatus:
        st = super().status()
        cfg = self.cfg
        st.fov_w_deg, st.fov_h_deg = cfg.fov_deg
        st.arcsec_per_px = cfg.arcsec_per_px
        with self._lock:
            if self._solution is not None:
                st.solution.CopyFrom(self._solution)
            st.solving = self._solving
        return st


def data_path(cfg: WidefieldSettings) -> Path:
    return Path(cfg.data_dir).expanduser() / "widefield"
