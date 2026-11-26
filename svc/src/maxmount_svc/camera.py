# SPDX-License-Identifier: GPL-3.0-or-later
"""Camera service for a stills camera over USB (the Nikon D500), via libgphoto2.

`mount-svc dslr` serves one camera on Zenoh (camera.proto; keys camera/<id>/status, frame, cmd):
settings (ISO, shutter speed, image quality), capture (the camera's shutter speed, or bulb
timed here), live view, and a stretched JPEG preview of each capture with a histogram. Files
go to the camera's card, the Pi (data_dir/<date>/), or both; each capture saved on the Pi gets
a JSON sidecar with the times, settings and what the mount was tracking. Video cameras (the
ZWO, zwo.py) also record SER video on the Pi (the record command), with a sidecar likewise.

One thread owns the camera (libgphoto2 is not thread-safe): commands are queued to it, and it
polls live view frames while nothing else is happening.

Backends: GphotoBackend (python-gphoto2: pip install 'maxmount-svc[dslr]') and SimBackend, a
synthetic star field for development and tests (--sim).

Real-time note: downloads allocate large buffers; on the Pi, freeing memory can disturb the
real-time core (see deploy/README.md). Keep raw files on the card (save_to card) while
tracking, and download afterwards.
"""
from __future__ import annotations

import io
import json
import logging
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .proto import mpb

log = logging.getLogger(__name__)
NS = 1_000_000_000
SETTINGS = ("iso", "shutterspeed", "imagequality")
LABELS = {"iso": "ISO", "shutterspeed": "Shutter", "imagequality": "Quality", "capturetarget": "Capture to"}


class CameraError(RuntimeError):
    pass


# -- image helpers -----------------------------------------------------------------------------
def _pil():
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - a dependency of maxmount-svc
        raise CameraError("Pillow is needed for previews (pip install pillow)") from e
    return Image


def frame_from_jpeg(jpeg: bytes, kind: str, stretch: bool, max_px: int = 1600) -> mpb.CameraFrame:
    """A frame from a camera's JPEG: downscaled to max_px on decode, then frame_from_array."""
    Image = _pil()
    im = Image.open(io.BytesIO(jpeg))
    im.draft("RGB", (max_px, max_px))  # fast JPEG downscale on decode
    im = im.convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px))
    return frame_from_array(np.asarray(im), kind, stretch)


def frame_from_array(a: np.ndarray, kind: str, stretch: bool, max_px: int = 1600, full_scale: float | None = None) -> mpb.CameraFrame:
    """Statistics and a JPEG preview of an image (H x W mono or H x W x 3 RGB; 8 or 16 bit).
    Statistics are on luminance scaled to 0-255 (histogram: 64 bins); saturated = within 2 %
    of full scale (255 or 65535 unless given). Stretch: asinh between the 0.5th and 99.9th
    percentiles."""
    Image = _pil()
    full = full_scale or (65535.0 if a.dtype == np.uint16 else 255.0)
    if max(a.shape[:2]) > max_px:  # decimate large frames (cheap and good enough for a preview)
        k = int(math.ceil(max(a.shape[:2]) / max_px))
        a = a[::k, ::k]
    lum = a.mean(axis=-1) if a.ndim == 3 else a
    lum8 = np.clip(lum.astype(np.float32) * (255.0 / full), 0, 255)
    hist = np.bincount(lum8.astype(np.uint8).ravel() >> 2, minlength=64)
    f = mpb.CameraFrame(t_ns=time.time_ns(), kind=kind, width=a.shape[1], height=a.shape[0], stretched=stretch,
                        median=float(np.median(lum8)), p99=float(np.percentile(lum8, 99)),
                        saturated_frac=float(np.mean(lum >= 0.98 * full)))
    f.histogram.extend(int(x) for x in hist)
    x = a.astype(np.float32) * (255.0 / full)
    if stretch:
        lo, hi = np.percentile(x, 0.5), np.percentile(x, 99.9)
        x = np.clip((x - lo) / max(hi - lo, 1e-3), 0, 1)
        k = 8.0
        x = np.arcsinh(k * x) / math.asinh(k) * 255
    im = Image.fromarray(np.clip(x, 0, 255).astype(np.uint8))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    f.jpeg = out.getvalue()
    return f


# -- backends -----------------------------------------------------------------------------------
@dataclass
class Shot:
    files: list[tuple[str, bytes | None]] = field(default_factory=list)  # (name, data or None if left on the card)
    preview_jpeg: bytes | None = None
    card_paths: list[str] = field(default_factory=list)
    preview_array: np.ndarray | None = None  # video cameras: the frame itself
    full_scale: float | None = None
    gain: float = 0.0
    bit_depth: int = 0
    note: str = ""


class SimBackend:
    """A synthetic camera: a fixed star field with noise that scales with ISO and exposure.
    Exposures really take their time, scaled by time_scale (tests use a small one)."""

    simulated = True
    SHUTTERS = ["1/4000", "1/1000", "1/250", "1/60", "1/15", "1/4", "1", "2", "5", "10", "30", "Bulb"]
    ISOS = ["100", "200", "400", "800", "1600", "3200", "6400", "12800"]
    QUALITY = ["JPEG Basic", "NEF (Raw)", "NEF+Basic"]

    def __init__(self, time_scale: float = 1.0, seed: int = 3):
        self.time_scale = time_scale
        self.values = {"iso": "1600", "shutterspeed": "2", "imagequality": "NEF+Basic", "capturetarget": "Memory card"}
        rng = np.random.default_rng(seed)
        self._stars = (rng.uniform(0, 1, (120, 2)), 10 ** rng.uniform(0, 2.5, 120))
        self._n = 0
        self._bulb_end = None

    def open(self) -> str:
        return "Nikon D500 (simulated)"

    def close(self) -> None:
        pass

    def battery(self) -> int:
        return 87

    def settings(self) -> list[dict]:
        ch = {"iso": self.ISOS, "shutterspeed": self.SHUTTERS, "imagequality": self.QUALITY,
              "capturetarget": ["Internal RAM", "Memory card"]}
        return [{"name": n, "value": self.values[n], "choices": ch[n]} for n in (*SETTINGS, "capturetarget")]

    def set(self, name: str, value: str) -> None:
        choices = {d["name"]: d["choices"] for d in self.settings()}
        if name not in choices or value not in choices[name]:
            raise CameraError(f"{name}: '{value}' not offered")
        self.values[name] = value

    bulb_supported = True

    def _image(self, w: int, h: int, exposure_s: float, jitter: float = 0.0) -> bytes:
        Image = _pil()
        gain = float(self.values["iso"]) / 1600 * max(exposure_s, 1e-3)
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        img = np.full((h, w), 12.0 + 6 * gain, np.float32)
        (pos, flux) = self._stars
        sigma = 1.6 * w / 1000
        for (u, v), fl in zip(pos, flux):
            cx, cy = u * w + jitter, v * h + jitter
            if 0 <= cx < w and 0 <= cy < h:
                x0, x1, y0, y1 = int(max(0, cx - 8 * sigma)), int(min(w, cx + 8 * sigma)), int(max(0, cy - 8 * sigma)), int(min(h, cy + 8 * sigma))
                img[y0:y1, x0:x1] += fl * gain * np.exp(-((x[y0:y1, x0:x1] - cx) ** 2 + (y[y0:y1, x0:x1] - cy) ** 2) / (2 * sigma**2))
        img += np.random.default_rng(self._n).normal(0, 2 + 2 * math.sqrt(float(self.values["iso"]) / 400), img.shape)
        rgb = np.repeat(np.clip(img, 0, 255).astype(np.uint8)[..., None], 3, axis=-1)
        out = io.BytesIO()
        Image.fromarray(rgb).save(out, "JPEG", quality=90)
        return out.getvalue()

    def preview(self) -> bytes:
        self._n += 1
        return self._image(640, 424, 0.5, jitter=(self._n % 10) * 0.3)

    def _exposure_s(self) -> float:
        s = self.values["shutterspeed"]
        return eval_shutter(s)

    def capture(self, bulb_s: float, abort: threading.Event, download_raw: bool) -> Shot:
        t = bulb_s if bulb_s > 0 else self._exposure_s()
        if abort.wait(t * self.time_scale) and bulb_s > 0:
            t = t * 0.5  # aborted early: shorter exposure
        self._n += 1
        jpg = self._image(1800, 1200, t)
        name = f"DSC_{self._n:04d}"
        q = self.values["imagequality"]
        shot = Shot(preview_jpeg=jpg)
        if "Basic" in q or "JPEG" in q:
            shot.files.append((name + ".JPG", jpg))
        if "NEF" in q:
            shot.files.append((name + ".NEF", b"NEF-SIM" + jpg if download_raw else None))
        if self.values["capturetarget"] == "Memory card":
            shot.card_paths = [f"/store_00010001/DCIM/100D500/{n}" for n, _d in shot.files]
        return shot


def eval_shutter(s: str) -> float:
    """'1/250' -> 0.004, '2' -> 2, '30"' -> 30; 0 if not a time (e.g. 'Bulb')."""
    s = s.strip().rstrip('"s')
    m = re.fullmatch(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1)) / float(m.group(2))
    try:
        return float(s)
    except ValueError:
        return 0.0


class GphotoBackend:
    """A real camera through python-gphoto2 (libgphoto2's PTP driver for Nikon)."""

    simulated = False
    NAMES = {  # config names to try, in order
        "iso": ("iso", "isospeed"),
        "shutterspeed": ("shutterspeed", "shutterspeed2"),
        "imagequality": ("imagequality", "imageformat"),
        "capturetarget": ("capturetarget",),
        "battery": ("batterylevel",),
        "bulb": ("bulb",),
        "viewfinder": ("viewfinder",),
    }

    def __init__(self):
        try:
            import gphoto2 as gp
        except ImportError as e:
            raise CameraError("python-gphoto2 is not installed: pip install 'maxmount-svc[dslr]'") from e
        self.gp = gp
        self.cam = None
        self.bulb_supported = False

    def open(self) -> str:
        gp = self.gp
        cam = gp.Camera()
        try:
            cam.init()
        except gp.GPhoto2Error as e:
            raise CameraError(f"no camera found over USB ({e})") from None
        self.cam = cam
        self.bulb_supported = self._widget("bulb") is not None
        try:
            return cam.get_abilities().model
        except gp.GPhoto2Error:
            return "camera"

    def close(self) -> None:
        if self.cam is not None:
            try:
                self.cam.exit()
            except self.gp.GPhoto2Error:
                pass
            self.cam = None

    def _widget(self, key: str):
        cfg = self.cam.get_config()
        for n in self.NAMES.get(key, (key,)):
            try:
                return cfg, cfg.get_child_by_name(n)
            except self.gp.GPhoto2Error:
                continue
        return None

    def battery(self) -> int:
        w = self._widget("battery")
        if w is None:
            return -1
        m = re.search(r"\d+", str(w[1].get_value()))
        return int(m.group()) if m else -1

    def settings(self):
        out = []
        for key in (*SETTINGS, "capturetarget"):
            w = self._widget(key)
            if w is None:
                continue
            wd = w[1]
            try:
                choices = [wd.get_choice(i) for i in range(wd.count_choices())]
            except self.gp.GPhoto2Error:
                choices = []
            out.append({"name": key, "value": str(wd.get_value()), "choices": choices, "read_only": bool(wd.get_readonly())})
        return out

    def set(self, name: str, value: str) -> None:
        w = self._widget(name)
        if w is None:
            raise CameraError(f"the camera has no '{name}' setting")
        cfg, wd = w
        try:
            if wd.get_type() == self.gp.GP_WIDGET_TOGGLE:  # bulb, viewfinder: 0/1
                value = int(value)
            wd.set_value(value)
            self.cam.set_config(cfg)
        except self.gp.GPhoto2Error as e:
            raise CameraError(f"{name} = {value}: {e}") from None

    def preview(self) -> bytes:
        cf = self.cam.capture_preview()
        return bytes(cf.get_data_and_size())

    def end_liveview(self) -> None:
        w = self._widget("viewfinder")
        if w is not None:
            try:
                w[1].set_value(0)
                self.cam.set_config(w[0])
            except self.gp.GPhoto2Error:
                pass

    def _collect(self, first, timeout_s: float):
        """The files a capture produced (RAW+JPEG gives two): the first path, then any
        FILE_ADDED events until CAPTURE_COMPLETE or the timeout."""
        gp = self.gp
        paths = [first] if first is not None else []
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            ev, data = self.cam.wait_for_event(500)
            if ev == gp.GP_EVENT_FILE_ADDED:
                paths.append(data)
            elif ev == gp.GP_EVENT_CAPTURE_COMPLETE and paths:
                break
            elif ev == gp.GP_EVENT_TIMEOUT and paths:
                break
        return paths

    def capture(self, bulb_s: float, abort: threading.Event, download_raw: bool) -> Shot:
        gp = self.gp
        if bulb_s > 0:
            if not self.bulb_supported:
                raise CameraError("this camera does not offer bulb control over USB")
            shutters = {d["name"]: d["choices"] for d in self.settings()}.get("shutterspeed", [])
            bulb = next((c for c in shutters if "bulb" in c.lower()), None)
            if bulb:
                self.set("shutterspeed", bulb)
            self.set("bulb", "1")
            abort.wait(bulb_s)
            self.set("bulb", "0")
            paths = self._collect(None, 30)
        else:
            first = self.cam.capture(gp.GP_CAPTURE_IMAGE)
            paths = self._collect(first, 5)
        shot = Shot()
        to_card = "card" in {d["name"]: d["value"] for d in self.settings()}.get("capturetarget", "").lower()
        for p in paths:
            name = p.name
            is_jpeg = name.lower().endswith((".jpg", ".jpeg"))
            if is_jpeg or download_raw or not to_card:
                data = bytes(self.cam.file_get(p.folder, name, gp.GP_FILE_TYPE_NORMAL).get_data_and_size())
                if not to_card:
                    try:
                        self.cam.file_delete(p.folder, name)  # free the camera's RAM
                    except gp.GPhoto2Error:
                        pass
            else:
                data = None
            shot.files.append((name, data))
            if to_card:
                shot.card_paths.append(f"{p.folder}/{name}")
            if is_jpeg and data:
                shot.preview_jpeg = data
        if shot.preview_jpeg is None and paths:
            # No JPEG (raw only): the camera's thumbnail of the first file.
            try:
                shot.preview_jpeg = bytes(self.cam.file_get(paths[0].folder, paths[0].name, gp.GP_FILE_TYPE_PREVIEW).get_data_and_size())
            except gp.GPhoto2Error:
                pass
        return shot


# -- the service ----------------------------------------------------------------------------------
class CameraService:
    def __init__(self, client, backend, camera_id: str = "dslr", data_dir: str | Path = "~/maxmount-data",
                 liveview_fps: float = 8.0):
        self.client = client
        self.backend = backend
        self.id = camera_id
        self.data_dir = Path(data_dir).expanduser() / camera_id
        self.liveview_fps = liveview_fps
        # Where captures can go: a DSLR has a card; a video camera saves on the Pi only.
        self.storage = tuple(getattr(backend, "storage", ("card", "pi", "both")))
        self._q: queue.Queue = queue.Queue()
        self._abort = threading.Event()
        self._closing = threading.Event()
        self._lock = threading.Lock()
        # Guarded by _lock.
        self._connected = False
        self._model = ""
        self._state = "disconnected"
        self._message = "starting"
        self._battery = -1
        self._settings: list = []
        self._liveview = False
        self._captures = 0
        self._exposure_s = 0.0
        self._exposure_end = 0
        self._save_to = self.storage[0]
        self._stretch = True
        self._svc_status = None
        self._threads: list[threading.Thread] = []
        self._decls = []

    def key(self, s: str) -> str:
        return self.client.key(f"camera/{self.id}/{s}")

    def start(self) -> None:
        s = self.client.session
        self._status_pub = s.declare_publisher(self.key("status"))
        self._frame_pub = s.declare_publisher(self.key("frame"))
        self._centroid_pub = s.declare_publisher(self.key("centroid"))
        if hasattr(self.backend, "on_measure"):
            self.backend.on_measure = self._on_measure
        self._decls = [s.declare_queryable(self.key("cmd"), self._on_cmd), self._status_pub, self._frame_pub, self._centroid_pub,
                       s.declare_subscriber(self.client.key("svc/status"), self._on_svc_status)]
        for fn in (self._camera_loop, self._status_loop):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self._threads.append(t)

    def close(self) -> None:
        self._closing.set()
        self._abort.set()
        for t in self._threads:
            t.join(timeout=10)
        for d in self._decls:
            try:
                d.undeclare()
            except Exception:  # noqa: BLE001
                pass

    # -- Zenoh --------------------------------------------------------------------------------
    def _on_svc_status(self, sample) -> None:
        st = mpb.ServiceStatus()
        try:
            st.ParseFromString(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._svc_status = st

    def _on_cmd(self, query) -> None:
        cmd = mpb.CameraCommand()
        try:
            cmd.ParseFromString(query.payload.to_bytes() if query.payload is not None else b"")
            ok, msg = self.handle(cmd)
        except Exception as e:  # noqa: BLE001 - always answer
            ok, msg = False, f"{type(e).__name__}: {e}"
        query.reply(self.key("cmd"), mpb.CameraReply(ok=ok, message=msg).SerializeToString())

    def handle(self, cmd) -> tuple[bool, str]:
        which = cmd.WhichOneof("command")
        if which == "abort":
            self._abort.set()
            return True, "abort requested"
        if which == "save_to":
            if cmd.save_to not in self.storage:
                return False, f"save_to must be {' or '.join(self.storage)}, not '{cmd.save_to}'"
            with self._lock:
                self._save_to = cmd.save_to
        elif which == "stretch":
            with self._lock:
                self._stretch = cmd.stretch
            return True, f"stretch {'on' if cmd.stretch else 'off'}"
        with self._lock:
            connected, state = self._connected, self._state
        if which == "save_to" and not connected:
            return True, f"will save to {cmd.save_to} when the camera connects"
        if not connected:
            return False, f"camera not connected ({self._message})"
        if which == "capture" and state in ("exposing", "downloading"):
            return False, "a capture is already in progress"
        done = threading.Event()
        result = {}
        self._q.put((which, getattr(cmd, which) if which else None, done, result))
        if which == "capture":
            return True, "capture started"  # long: report progress through the status
        if not done.wait(15):
            return False, "camera busy"
        return result.get("ok", False), result.get("message", "")

    # -- camera thread -------------------------------------------------------------------------
    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)
        self._publish_status()

    def _refresh(self) -> None:
        settings = self.backend.settings()
        battery = self.backend.battery()
        with self._lock:
            self._settings, self._battery = settings, battery

    def _apply_save_to(self) -> None:
        """capturetarget follows save_to: card or both -> the memory card; pi -> internal RAM."""
        with self._lock:
            save_to = self._save_to
        want = "card" if save_to in ("card", "both") else "ram"
        cur = next((d for d in self._settings if d["name"] == "capturetarget"), None)
        if cur and want not in cur["value"].lower():
            target = next((c for c in cur["choices"] if want in c.lower()), None)
            if target:
                self.backend.set("capturetarget", target)

    def _camera_loop(self) -> None:
        last_try = 0.0
        while not self._closing.is_set():
            if not self._connected:
                if time.monotonic() - last_try < 5:
                    time.sleep(0.2)
                    continue
                last_try = time.monotonic()
                try:
                    model = self.backend.open()
                    self._refresh()
                    self._set(connected=True, model=model, state="idle", message=f"{model} connected")
                except CameraError as e:
                    self._set(connected=False, state="disconnected", message=str(e))
                continue
            try:
                item = self._q.get(timeout=1 / self.liveview_fps if self._liveview else 0.5)
            except queue.Empty:
                item = None
            try:
                if item:
                    self._run(*item)
                elif self._liveview:
                    self._publish_frame(self._liveview_frame())
                else:
                    self._refresh()
            except CameraError as e:
                self._set(state="error", message=str(e))
            except Exception as e:  # noqa: BLE001 - usually the camera went away
                log.warning("camera: %s", e)
                try:
                    self.backend.close()
                except Exception:  # noqa: BLE001
                    pass
                self._set(connected=False, liveview=False, state="disconnected", message=f"camera lost: {e}")

    def _run(self, which, arg, done, result) -> None:
        try:
            if which == "set":
                self.backend.set(arg.name, arg.value)
                self._refresh()
                result.update(ok=True, message=f"{LABELS.get(arg.name, arg.name)} {arg.value}")
            elif which == "save_to":
                self._apply_save_to()
                self._refresh()
                result.update(ok=True, message=f"saving to {self._save_to}")
            elif which == "liveview":
                if hasattr(self.backend, "set_liveview"):
                    self.backend.set_liveview(bool(arg))  # a video camera starts or stops streaming
                elif not arg and self._liveview and hasattr(self.backend, "end_liveview"):
                    self.backend.end_liveview()
                self._set(liveview=bool(arg), state="liveview" if arg else "idle")
                result.update(ok=True, message=f"live view {'on' if arg else 'off'}")
            elif which == "capture":
                self._capture(arg.bulb_s)
                result.update(ok=True, message="captured")
            elif which == "record":
                result.update(ok=True, message=self._record(arg))
            elif which == "roi_track":
                result.update(ok=True, message=self._roi_track(arg))
        except CameraError as e:
            self._set(state="error" if which == "capture" else self._state, message=str(e))
            result.update(ok=False, message=str(e))
        finally:
            done.set()

    def _capture(self, bulb_s: float) -> None:
        self._apply_save_to()
        values = {d["name"]: d["value"] for d in self._settings}
        exposure = bulb_s if bulb_s > 0 else (self.backend.exposure_s() if hasattr(self.backend, "exposure_s")
                                               else eval_shutter(values.get("shutterspeed", "")))
        was_live = self._liveview
        if was_live and hasattr(self.backend, "end_liveview"):
            self.backend.end_liveview()
        self._abort.clear()
        t0 = time.time_ns()
        self._set(state="exposing", exposure_s=exposure, exposure_end=t0 + int(exposure * NS),
                  message=f"exposing {exposure:g} s" if exposure else "exposing")
        with self._lock:
            save_to, svc = self._save_to, self._svc_status
        shot = self.backend.capture(bulb_s, self._abort, download_raw=save_to in ("pi", "both"))
        t1 = time.time_ns()
        self._set(state="downloading", message="downloading")
        saved = []
        if save_to in ("pi", "both"):
            day = self.data_dir / time.strftime("%Y%m%d", time.gmtime(t0 / NS))
            day.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t0 / NS))
            for name, data in shot.files:
                if data is None:
                    continue
                p = day / f"{stem}_{name}"
                p.write_bytes(data)
                saved.append(str(p))
            if saved:
                side = {
                    "camera": self._model, "start_utc_ns": t0, "end_utc_ns": t1, "exposure_s": exposure,
                    "bulb": bulb_s > 0, "settings": values, "files": [Path(p).name for p in saved],
                    "card_paths": shot.card_paths,
                    "note": "times are when the USB trigger was sent and the capture returned (tens of ms jitter)",
                }
                if svc is not None and svc.target_name:
                    side["mount"] = {"target": svc.target_name, "kind": svc.target_kind, "state": mpb.ServiceState.Name(svc.state),
                                     "target_az_deg": svc.target_az_deg, "target_el_deg": svc.target_el_deg}
                (day / f"{stem}.json").write_text(json.dumps(side, indent=1))
        files = saved + [f"card:{p}" for p in shot.card_paths]
        with self._lock:
            self._captures += 1
            n = self._captures
            stretch = self._stretch
        if shot.preview_array is not None or shot.preview_jpeg:
            f = (frame_from_array(shot.preview_array, "capture", stretch, full_scale=shot.full_scale)
                 if shot.preview_array is not None else frame_from_jpeg(shot.preview_jpeg, "capture", stretch))
            f.gain, f.bit_depth = shot.gain, shot.bit_depth
            f.files.extend(files)
            f.exposure_s, f.iso, f.shutter = exposure, values.get("iso", ""), values.get("shutterspeed", "")
            self._publish_frame(f)
        self._refresh()
        where = {"card": "on the card", "pi": "on the Pi", "both": "on the card and the Pi"}[save_to]
        if shot.note:
            where += f" ({shot.note})"
        self._set(state="liveview" if was_live else "idle", exposure_s=0.0, exposure_end=0,
                  message=f"capture {n}: {len(shot.files)} file(s) {where}")

    def _roi_track(self, arg) -> str:
        b = self.backend
        if not hasattr(b, "set_roi_track"):
            raise CameraError("this camera has no ROI tracking")
        pick = None
        if arg.has_pick:
            if not (0 <= arg.pick_x <= 1 and 0 <= arg.pick_y <= 1):
                raise CameraError("pick_x and pick_y are fractions of the ROI (0..1)")
            pick = (arg.pick_x, arg.pick_y)
        if arg.on and not self._liveview:
            b.set_liveview(True)
            self._set(liveview=True, state="liveview")
        b.set_roi_track(arg.on, pick, arg.recenter)
        if arg.on:
            msg = "ROI tracking: acquiring " + ("the object picked" if pick else "the brightest object")
        else:
            msg = "ROI tracking off"
        if arg.recenter:
            msg += "; ROI back to the sensor centre"
        self._set(message=msg)
        return msg

    def _on_measure(self, m: dict) -> None:
        """A tracking measurement from the backend's video thread -> camera/<id>/centroid."""
        c = mpb.CameraCentroid(t_ns=int(m["t_ns"]), camera_id=self.id, state=m["state"], x=m["x"], y=m["y"], dx=m["dx"],
                               dy=m["dy"], bin=int(m["bin"]), flux=m["flux"], snr=m["snr"], vx=m["vx"], vy=m["vy"],
                               pixel_um=m["pixel_um"])
        c.roi.extend(int(v) for v in m["roi"])
        try:
            self._centroid_pub.put(c.SerializeToString())
        except Exception:  # noqa: BLE001
            pass

    def _mount_snapshot(self) -> dict | None:
        with self._lock:
            svc = self._svc_status
        if svc is None or not svc.target_name:
            return None
        return {"target": svc.target_name, "kind": svc.target_kind, "state": mpb.ServiceState.Name(svc.state),
                "target_az_deg": svc.target_az_deg, "target_el_deg": svc.target_el_deg}

    def _record(self, arg) -> str:
        """Start or stop SER recording of the video stream (video cameras)."""
        b = self.backend
        if not getattr(b, "records_video", False):
            raise CameraError("this camera cannot record video")
        if not arg.on:
            rec = b.stop_record("stopped")
            if rec is None:
                return "not recording"
            left = rec.queued
            return f"recording stopped; writing {left} queued frames" if left else "recording stopped"
        if arg.duration_s < 0 or arg.max_frames < 0:
            raise CameraError("duration and frame limit must be >= 0")
        if not self._liveview:
            b.set_liveview(True)
            self._set(liveview=True, state="liveview")
        t0 = time.time_ns()
        day = self.data_dir / time.strftime("%Y%m%d", time.gmtime(t0 / NS))
        stem = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t0 / NS))
        values = {d["name"]: d["value"] for d in self._settings}
        side = {"camera": self._model, "start_utc_ns": t0, "settings": values, "duration_limit_s": arg.duration_s,
                "frame_limit": arg.max_frames,
                "note": "SER timestamps are the start of each exposure: frame arrival on the Pi minus the exposure (approximate)"}
        mount = self._mount_snapshot()
        if mount:
            side["mount_at_start"] = mount

        def done(rec):
            side.update(file=rec.path.name, frames=rec.frames, dropped=rec.dropped, end_utc_ns=rec.end_ns,
                        elapsed_s=round(rec.elapsed_s, 3), bytes=rec.writer.bytes_written, stopped_because=rec.reason)
            try:
                rec.path.with_suffix(".json").write_text(json.dumps(side, indent=1))
            except OSError as e:
                log.warning("camera: sidecar: %s", e)
            fps = rec.frames / rec.elapsed_s if rec.elapsed_s > 0 else 0.0
            msg = (f"recorded {rec.frames} frames ({rec.writer.bytes_written / 1e6:.0f} MB, {rec.elapsed_s:.1f} s, "
                   f"{fps:.1f} fps) to {rec.path.name} on the Pi: {rec.reason}")
            if rec.dropped:
                msg += f"; {rec.dropped} frames dropped (disk too slow)"
            self._set(message=msg)

        rec = b.start_record(day, stem, max_frames=arg.max_frames, max_s=arg.duration_s, on_done=done)
        limits = [f"{arg.duration_s:g} s" if arg.duration_s else "", f"{arg.max_frames} frames" if arg.max_frames else ""]
        limit = " or ".join(x for x in limits if x) or "until stopped"
        msg = f"recording to {rec.path.name} on the Pi ({limit})"
        self._set(message=msg)
        return msg

    def _liveview_frame(self):
        """The backend's latest live frame: a JPEG (DSLR live view) or an array (video camera)."""
        with self._lock:
            stretch = self._stretch
        a = getattr(self.backend, "preview_array", None)
        if a is not None:
            img, full = a()
            f = frame_from_array(img, "liveview", stretch, 1024, full_scale=full)
            info = getattr(self.backend, "preview_info", None) or {}
            if info.get("roi"):
                f.roi.extend(int(v) for v in info["roi"])
                f.track_state = info.get("state", "")
                f.track_x, f.track_y = float(info.get("x", math.nan)), float(info.get("y", math.nan))
            return f
        return frame_from_jpeg(self.backend.preview(), "liveview", stretch, 1024)

    # -- publishing ----------------------------------------------------------------------------
    def status(self) -> mpb.CameraStatus:
        with self._lock:
            st = mpb.CameraStatus(
                t_ns=time.time_ns(), camera_id=self.id, connected=self._connected, model=self._model, state=self._state,
                message=self._message, battery_percent=self._battery, liveview=self._liveview, captures=self._captures,
                exposure_s=self._exposure_s, exposure_end_ns=self._exposure_end, save_to=self._save_to,
                data_dir=str(self.data_dir), simulated=bool(getattr(self.backend, "simulated", False)),
                stretch=self._stretch, bulb_supported=bool(getattr(self.backend, "bulb_supported", False)),
                records_video=bool(getattr(self.backend, "records_video", False)),
            )
            for d in self._settings:
                st.settings.add(name=d["name"], label=d.get("label") or LABELS.get(d["name"], d["name"]), value=str(d["value"]),
                                choices=d.get("choices") or [], read_only=bool(d.get("read_only")), numeric=bool(d.get("numeric")),
                                min=float(d.get("min", 0)), max=float(d.get("max", 0)), step=float(d.get("step", 0)),
                                unit=str(d.get("unit", "")))
            st.storage.extend(self.storage)
        extra = getattr(self.backend, "extra_status", None)
        if extra:
            try:
                x = extra()
                st.fps, st.dropped = float(x.get("fps", 0)), int(x.get("dropped", 0))
                st.temperature_c = float(x.get("temperature_c", float("nan")))
                st.roi.extend(int(v) for v in x.get("roi", []))
                st.sensor_width, st.sensor_height = int(x.get("sensor_width", 0)), int(x.get("sensor_height", 0))
                st.recording = bool(x.get("recording", False))
                if "record_file" in x:
                    st.record_file = str(x["record_file"])
                    st.record_frames, st.record_dropped = int(x["record_frames"]), int(x["record_dropped"])
                    st.record_elapsed_s, st.record_bytes = float(x["record_elapsed_s"]), int(x["record_bytes"])
                    st.record_queued, st.record_pool = int(x["record_queued"]), int(x["record_pool"])
                    st.disk_free_bytes = int(x["disk_free_bytes"])
                    st.record_max_s, st.record_max_frames = float(x["record_max_s"]), int(x["record_max_frames"])
                    st.record_stopping = bool(x.get("record_stopping", False))
                if "track_state" in x:
                    st.roi_tracking, st.track_state = bool(x["roi_tracking"]), str(x["track_state"])
                    st.track_x, st.track_y = float(x["track_x"]), float(x["track_y"])
                    st.track_err_x, st.track_err_y = float(x["track_err_x"]), float(x["track_err_y"])
                    st.track_snr, st.track_speed = float(x["track_snr"]), float(x["track_speed"])
                    st.roi_moves, st.roi_centered = int(x["roi_moves"]), bool(x.get("roi_centered", True))
            except Exception:  # noqa: BLE001 - status must not fail
                pass
        return st

    def _publish_status(self) -> None:
        try:
            self._status_pub.put(self.status().SerializeToString())
        except Exception:  # noqa: BLE001
            pass

    def _publish_frame(self, f) -> None:
        f.camera_id = self.id
        self._frame_pub.put(f.SerializeToString())

    def _status_loop(self) -> None:
        while not self._closing.wait(1.0):
            self._publish_status()
