# SPDX-License-Identifier: GPL-3.0-or-later
"""ZWO ASI camera backends for the camera service (`mount-svc zwo`; camera id "zwo").

Video streaming with a region of interest (ROI), binning, exposure, gain, offset, image type
and USB bandwidth; single frames saved as FITS on the Pi. Live view previews are debayered
(2x2 superpixel) and stretched by the camera service.

Backends:
  ZwoSdkBackend  a real camera through ZWO's ASI SDK (libASICamera2, a binary library from
                 ZWO's developer page, loaded with the MIT-licensed `zwoasi` package:
                 pip install 'maxmount-svc[zwo]'; cameras.zwo.sdk_lib in the system file).
  ZwoSimBackend  an ASI224MC simulator (1304 x 976, RGGB, 12-bit): a star field and a bright
                 extended target that wanders like an imperfectly tracked satellite, with
                 shot and read noise, a gain model and a USB frame-rate model.

One video thread per backend grabs frames into reused buffers (on the Pi, allocating and
freeing large buffers disturbs the real-time core: see deploy/README.md).
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from .camera import CameraError, Shot

NS = 1_000_000_000
ROI_PRESETS = ["full", "1024x768", "800x600", "640x480", "512x512", "320x240", "256x256", "128x128", "64x64"]
IMAGE_TYPES = ["RAW8", "RAW16", "RGB24"]


# -- FITS -------------------------------------------------------------------------------------
def fits_bytes(data: np.ndarray, header: dict) -> bytes:
    """A minimal FITS file: 8-bit unsigned or 16-bit (BZERO 32768) data, mono (H x W) or RGB
    (3 planes, H x W x 3 in). Rows are written in array order (ROWORDER = TOP-DOWN)."""
    if data.ndim == 3:
        data = np.moveaxis(data, -1, 0)  # planes first: NAXIS3 = 3
    if data.dtype == np.uint8:
        bitpix, raw = 8, data.astype(">u1")
        extra = {}
    elif data.dtype == np.uint16:
        bitpix, raw = 16, (data.astype(np.int32) - 32768).astype(">i2")
        extra = {"BZERO": 32768, "BSCALE": 1}
    else:
        raise ValueError(f"unsupported dtype {data.dtype}")
    cards = [("SIMPLE", True, "conforms to FITS"), ("BITPIX", bitpix, ""), ("NAXIS", data.ndim, "")]
    for i, n in enumerate(reversed(data.shape)):
        cards.append((f"NAXIS{i + 1}", int(n), ""))
    cards += [(k, v, "") for k, v in extra.items()]
    cards += [("ROWORDER", "TOP-DOWN", "first row is the top of the image")]
    cards += [(k, v, "") for k, v in header.items()]

    def card(k, v, c):
        if isinstance(v, bool):
            val = f"{'T' if v else 'F':>20}"
        elif isinstance(v, (int, np.integer)):
            val = f"{int(v):>20}"
        elif isinstance(v, (float, np.floating)):
            val = f"{float(v):>20.10G}"
        else:
            val = "'" + str(v).replace("'", "''")[:68].ljust(8) + "'"
        s = f"{k:<8}= {val}" + (f" / {c}" if c else "")
        return s[:80].ljust(80)

    head = "".join(card(*c) for c in cards) + "END".ljust(80)
    head += " " * (-len(head) % 2880)
    body = raw.tobytes()
    return head.encode("ascii") + body + b"\0" * (-len(body) % 2880)


def superpixel_rgb(raw: np.ndarray) -> np.ndarray:
    """RGGB Bayer mosaic -> RGB at half resolution (cheap, for previews)."""
    h, w = raw.shape[0] // 2 * 2, raw.shape[1] // 2 * 2
    a = raw[:h, :w].astype(np.float32)
    r, g1, g2, b = a[0::2, 0::2], a[0::2, 1::2], a[1::2, 0::2], a[1::2, 1::2]
    return np.stack([r, (g1 + g2) / 2, b], axis=-1).astype(raw.dtype)


# -- shared base: settings, ROI, video thread ------------------------------------------------
class _ZwoBase:
    storage = ("pi",)
    bulb_supported = False

    def __init__(self):
        self.model = "ZWO"
        self.sensor = (1304, 976)
        self.pixel_um = 3.75
        self.color = True
        self.bins = [1, 2]
        self.types = list(IMAGE_TYPES)
        self.limits = {"gain": (0, 510, 1), "offset": (0, 255, 1), "bandwidth": (40, 100, 1), "exposure_ms": (0.032, 60000, 0.001)}
        self.v = {"exposure_ms": 10.0, "gain": 200.0, "offset": 8.0, "bandwidth": 80.0, "image_type": "RAW8", "bin": "1", "roi": "640x480"}
        self._lock = threading.Lock()
        self._video = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest: np.ndarray | None = None  # the newest frame (a reused buffer, copied out under the lock)
        self._latest_t = 0
        self._seq = 0
        self._fps = 0.0
        self._dropped = 0
        self._temperature = float("nan")

    # ROI in binned pixels, centred, with the SDK's alignment (width % 8, height % 2, even origin).
    def roi(self) -> tuple[int, int, int, int]:
        b = int(self.v["bin"])
        fw, fh = self.sensor[0] // b // 8 * 8, self.sensor[1] // b // 2 * 2
        if self.v["roi"] == "full":
            return 0, 0, fw, fh
        w, h = (int(x) for x in self.v["roi"].split("x"))
        w, h = min(w, fw) // 8 * 8, min(h, fh) // 2 * 2
        x, y = (fw - w) // 2 // 2 * 2, (fh - h) // 2 // 2 * 2
        return x, y, w, h

    def settings(self) -> list[dict]:
        def num(name, label, unit):
            lo, hi, step = self.limits[name]
            return {"name": name, "label": label, "value": f"{self.v[name]:g}", "numeric": True, "min": lo, "max": hi, "step": step, "unit": unit}
        fw, fh = self.sensor[0] // int(self.v["bin"]), self.sensor[1] // int(self.v["bin"])
        rois = [r for r in ROI_PRESETS if r == "full" or (int(r.split("x")[0]) <= fw and int(r.split("x")[1]) <= fh)]
        return [
            num("exposure_ms", "Exposure", "ms"), num("gain", "Gain", ""), num("offset", "Offset", ""),
            {"name": "image_type", "label": "Format", "value": self.v["image_type"], "choices": self.types},
            {"name": "bin", "label": "Bin", "value": self.v["bin"], "choices": [str(b) for b in self.bins]},
            {"name": "roi", "label": "ROI", "value": self.v["roi"], "choices": rois},
            num("bandwidth", "USB", "%"),
        ]

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
        elif name == "image_type" and value in self.types:
            new = value
        elif name == "bin" and value in [str(b) for b in self.bins]:
            new = value
        elif name == "roi" and value in ROI_PRESETS:
            new = value
        else:
            raise CameraError(f"{name}: '{value}' not offered")
        was = self._video
        geometry = name in ("image_type", "bin", "roi")
        if was and geometry:
            self._stop_video()
        self.v[name] = new
        self._apply(name)
        if was and geometry:
            self._start_video()

    def battery(self) -> int:
        return -1

    def exposure_s(self) -> float:
        return self.v["exposure_ms"] / 1000

    def full_scale(self) -> float:
        return 255.0 if self.v["image_type"] in ("RAW8", "RGB24") else 65535.0

    def extra_status(self) -> dict:
        x, y, w, h = self.roi()
        return {"fps": self._fps if self._video else 0.0, "dropped": self._dropped, "temperature_c": self._temperature,
                "roi": [x, y, w, h], "sensor_width": self.sensor[0], "sensor_height": self.sensor[1]}

    # -- video ------------------------------------------------------------------------------
    def set_liveview(self, on: bool) -> None:
        if on and not self._video:
            self._start_video()
        elif not on and self._video:
            self._stop_video()

    def _start_video(self) -> None:
        self._stop.clear()
        with self._lock:
            self._latest = None  # a frame from before (another ROI or format) must not be shown
            self._seq, self._fps = 0, 0.0
        self._begin_video()
        self._video = True
        self._thread = threading.Thread(target=self._video_loop, daemon=True, name="zwo-video")
        self._thread.start()

    def _stop_video(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._video = False
        self._end_video()

    def _video_loop(self) -> None:
        t_last, n = time.monotonic(), 0
        while not self._stop.is_set():
            try:
                frame = self._grab_video_frame()
            except Exception:  # noqa: BLE001 - a timeout; the next one may succeed
                if self._stop.is_set():
                    break
                time.sleep(0.01)
                continue
            with self._lock:
                if self._latest is None or self._latest.shape != frame.shape or self._latest.dtype != frame.dtype:
                    self._latest = np.empty_like(frame)
                np.copyto(self._latest, frame)
                self._latest_t = time.time_ns()
                self._seq += 1
            n += 1
            now = time.monotonic()
            if now - t_last >= 1.0:
                self._fps, t_last, n = n / (now - t_last), now, 0

    def _latest_copy(self, after_seq: int = -1, timeout_s: float = 5.0):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            with self._lock:
                if self._latest is not None and self._seq > after_seq:
                    return self._latest.copy(), self._latest_t, self._seq
            time.sleep(0.002)
        raise CameraError("no frame from the camera")

    def _display(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 2 and self.color and self.v["image_type"] != "RGB24":
            return superpixel_rgb(frame)
        return frame

    def preview_array(self):
        """The latest live frame for display, and its full scale."""
        if not self._video:
            raise CameraError("live view is off")
        frame, _t, _s = self._latest_copy(timeout_s=max(2.0, self.v["exposure_ms"] / 1000 * 2 + 1))
        return self._display(frame), self.full_scale()

    def capture(self, bulb_s: float, abort: threading.Event, download_raw: bool) -> Shot:
        """One frame at full bit depth, as FITS: the next video frame while streaming, else a
        single exposure."""
        exp_s = self.v["exposure_ms"] / 1000
        if self._video:
            with self._lock:
                seq = self._seq
            frame, t_end, _ = self._latest_copy(after_seq=seq, timeout_s=exp_s * 2 + 5)
            note = "from the video stream"
        else:
            frame = self._snapshot(abort)
            t_end = time.time_ns()
            note = "single exposure"
        t_start = t_end - int(exp_s * NS)
        x, y, w, h = self.roi()
        b = int(self.v["bin"])
        hdr = {
            "DATE-OBS": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start / NS)) + f".{t_start % NS // 1_000_000:03d}",
            "EXPTIME": exp_s, "GAIN": float(self.v["gain"]), "OFFSET": float(self.v["offset"]),
            "XBINNING": b, "YBINNING": b, "XPIXSZ": self.pixel_um * b, "YPIXSZ": self.pixel_um * b,
            "XORGSUBF": x, "YORGSUBF": y, "INSTRUME": self.model, "IMAGETYP": "Light Frame",
            "TIMESYS": "UTC", "TIMENOTE": "approximate: frame arrival minus exposure",
        }
        if math.isfinite(self._temperature):
            hdr["CCD-TEMP"] = round(self._temperature, 1)
        if self.color and frame.ndim == 2:
            hdr.update({"BAYERPAT": "RGGB", "XBAYROFF": 0, "YBAYROFF": 0})
        name = f"{self.v['image_type']}_{w}x{h}_{self.v['exposure_ms']:g}ms.fits"
        bits = 8 if frame.dtype == np.uint8 else 16
        return Shot(files=[(name, fits_bytes(frame, hdr))], preview_array=self._display(frame), full_scale=self.full_scale(),
                    gain=float(self.v["gain"]), bit_depth=bits, note=note)

    # Implemented by the backends.
    def _apply(self, name: str) -> None: ...
    def _begin_video(self) -> None: ...
    def _end_video(self) -> None: ...
    def _grab_video_frame(self) -> np.ndarray: ...
    def _snapshot(self, abort: threading.Event) -> np.ndarray: ...


# -- simulator --------------------------------------------------------------------------------
class ZwoSimBackend(_ZwoBase):
    """An ASI224MC on the C8: stars, sky glow and a bright extended target that wanders around
    the centre (like a satellite with tracking error), with photon and read noise. Gain is
    in the SDK's 0.1 dB units (e/ADU = 3.6 * 10^(-gain / 200)); 12-bit ADC."""

    simulated = True
    MAX_FPS = 60  # the simulator's own limit (frames are computed in numpy)

    def __init__(self, seed: int = 7):
        super().__init__()
        self.model = "ZWO ASI224MC (simulated)"
        self._temperature = 24.5
        rng = np.random.default_rng(seed)
        w, h = self.sensor
        sky = np.full((h, w), 2.0, np.float32)  # e-/px/ms of sky
        y, x = np.mgrid[0:h, 0:w]
        for _ in range(60):
            cx, cy, f = rng.uniform(0, w), rng.uniform(0, h), 10 ** rng.uniform(1.5, 3.5)
            x0, x1, y0, y1 = int(max(cx - 8, 0)), int(min(cx + 8, w)), int(max(cy - 8, 0)), int(min(cy + 8, h))
            sky[y0:y1, x0:x1] += f * np.exp(-((x[y0:y1, x0:x1] - cx) ** 2 + (y[y0:y1, x0:x1] - cy) ** 2) / (2 * 1.6**2))
        self._sky = sky
        self._rng = rng
        self._t0 = time.monotonic()
        self._next = 0.0
        bayer = np.ones((h, w), np.float32)
        bayer[0::2, 0::2], bayer[1::2, 1::2] = 0.8, 0.65
        self._bayer = bayer

    def open(self) -> str:
        return self.model

    def close(self) -> None:
        if self._video:
            self._stop_video()

    def _frame(self) -> np.ndarray:
        x, y, w, h = self.roi()
        b = int(self.v["bin"])
        exp_ms = self.v["exposure_ms"]
        sx, sy, sw, sh = x * b, y * b, w * b, h * b
        e = self._sky[sy:sy + sh, sx:sx + sw].copy()
        # The target: a body and solar panels, wandering ~40 px around the centre.
        t = time.monotonic() - self._t0
        cx = self.sensor[0] / 2 + 40 * math.sin(0.7 * t) - sx
        cy = self.sensor[1] / 2 + 25 * math.cos(0.9 * t) - sy
        yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
        e += 400 * np.exp(-((xx - cx) ** 2 / 50 + (yy - cy) ** 2 / 18))
        e += 250 * np.exp(-((xx - cx) ** 2 / 900 + (yy - cy) ** 2 / 4)) * (np.abs(xx - cx) > 6)
        e *= exp_ms * (self._bayer[sy:sy + sh, sx:sx + sw] if self.color else 1.0)
        if b > 1:
            e = e.reshape(h, b, w, b).sum(axis=(1, 3))
        e = e + self._rng.normal(0, 1, e.shape).astype(np.float32) * np.sqrt(e + 4.0)  # photons + 2 e- read noise
        egain = 3.6 * 10 ** (-self.v["gain"] / 200)
        adu = np.clip(e / egain + self.v["offset"] * 4, 0, 4095)
        t_ = self.v["image_type"]
        if t_ == "RAW16":
            return (adu.astype(np.uint16) << 4)
        a8 = (adu / 16).astype(np.uint8)
        if t_ == "RGB24":
            return superpixel_rgb(a8).repeat(2, 0).repeat(2, 1)[:h, :w] if self.color else np.repeat(a8[..., None], 3, -1)
        return a8

    def _interval(self) -> float:
        x, y, w, h = self.roi()
        bpp = 2 if self.v["image_type"] == "RAW16" else 3 if self.v["image_type"] == "RGB24" else 1
        usb = 2.0e8 * self.v["bandwidth"] / 100  # bytes/s; full-frame RAW8 ~150 fps on a real ASI224
        return max(self.v["exposure_ms"] / 1000, w * h * bpp / usb, 1 / self.MAX_FPS)

    def _grab_video_frame(self) -> np.ndarray:
        now = time.monotonic()
        wait = self._next - now
        if wait > 0 and self._stop.wait(wait):
            raise CameraError("stopped")
        self._next = max(self._next, now) + self._interval()
        return self._frame()

    def _snapshot(self, abort: threading.Event) -> np.ndarray:
        abort.wait(min(self.v["exposure_ms"] / 1000, 30))
        return self._frame()


# -- the real camera --------------------------------------------------------------------------
class ZwoSdkBackend(_ZwoBase):
    """A ZWO camera through the ASI SDK (libASICamera2) and `zwoasi`."""

    simulated = False

    def __init__(self, sdk_lib: str):
        super().__init__()
        try:
            import zwoasi as asi
        except ImportError as e:
            raise CameraError("zwoasi is not installed: pip install 'maxmount-svc[zwo]'") from e
        self.asi = asi
        self.sdk_lib = sdk_lib
        self.cam = None
        self._buf: bytearray | None = None

    def open(self) -> str:
        asi = self.asi
        try:
            asi.init(self.sdk_lib)
        except asi.ZWO_Error as e:
            if "already" not in str(e).lower():
                raise CameraError(f"ASI SDK {self.sdk_lib}: {e}") from None
        except OSError as e:
            raise CameraError(f"cannot load the ASI SDK library '{self.sdk_lib}' ({e}); set cameras.zwo.sdk_lib") from None
        if asi.get_num_cameras() == 0:
            raise CameraError("no ZWO camera found over USB")
        cam = asi.Camera(0)
        info = cam.get_camera_property()
        self.model = str(info.get("Name", "ZWO"))
        self.sensor = (int(info["MaxWidth"]), int(info["MaxHeight"]))
        self.pixel_um = float(info.get("PixelSize", 3.75))
        self.color = bool(info.get("IsColorCam", True))
        self.bins = [int(b) for b in info.get("SupportedBins", [1, 2]) if b]
        fmts = {asi.ASI_IMG_RAW8: "RAW8", asi.ASI_IMG_RAW16: "RAW16", asi.ASI_IMG_RGB24: "RGB24"}
        self.types = [fmts[f] for f in info.get("SupportedVideoFormat", []) if f in fmts] or ["RAW8", "RAW16"]
        if not self.color and "RGB24" in self.types:
            self.types.remove("RGB24")
        ctl = cam.get_controls()
        for key, name in (("gain", "Gain"), ("offset", "Offset"), ("bandwidth", "BandWidth")):
            c = ctl.get(name) or (ctl.get("Brightness") if key == "offset" else None)
            if c:
                self.limits[key] = (float(c["MinValue"]), float(c["MaxValue"]), 1)
                self.v[key] = min(max(self.v[key], self.limits[key][0]), self.limits[key][1])
        if "Exposure" in ctl:
            c = ctl["Exposure"]
            self.limits["exposure_ms"] = (c["MinValue"] / 1000, c["MaxValue"] / 1000, 0.001)
        self.cam = cam
        for name in ("gain", "offset", "bandwidth", "exposure_ms", "roi"):
            self._apply(name)
        self._read_temperature()
        return self.model

    def close(self) -> None:
        if self._video:
            self._stop_video()
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception:  # noqa: BLE001
                pass
            self.cam = None

    def _read_temperature(self) -> None:
        try:
            self._temperature = self.cam.get_control_value(self.asi.ASI_TEMPERATURE)[0] / 10
        except Exception:  # noqa: BLE001
            pass

    def settings(self) -> list[dict]:
        if self.cam is not None and not self._video:
            self._read_temperature()
        return super().settings()

    def _apply(self, name: str) -> None:
        asi, cam = self.asi, self.cam
        if cam is None:
            return
        try:
            if name == "gain":
                cam.set_control_value(asi.ASI_GAIN, int(self.v["gain"]))
            elif name == "offset":
                cam.set_control_value(getattr(asi, "ASI_OFFSET", asi.ASI_BRIGHTNESS), int(self.v["offset"]))
            elif name == "bandwidth":
                cam.set_control_value(asi.ASI_BANDWIDTHOVERLOAD, int(self.v["bandwidth"]))
            elif name == "exposure_ms":
                cam.set_control_value(asi.ASI_EXPOSURE, int(round(self.v["exposure_ms"] * 1000)))
            else:  # image_type, bin, roi
                x, y, w, h = self.roi()
                it = {"RAW8": asi.ASI_IMG_RAW8, "RAW16": asi.ASI_IMG_RAW16, "RGB24": asi.ASI_IMG_RGB24}[self.v["image_type"]]
                cam.set_roi(start_x=x, start_y=y, width=w, height=h, bins=int(self.v["bin"]), image_type=it)
                bpp = {"RAW8": 1, "RAW16": 2, "RGB24": 3}[self.v["image_type"]]
                self._buf = bytearray(w * h * bpp)  # reused for every frame
        except asi.ZWO_Error as e:
            raise CameraError(f"{name}: {e}") from None

    def _begin_video(self) -> None:
        self.cam.start_video_capture()

    def _end_video(self) -> None:
        try:
            self._dropped = int(self.cam.get_dropped_frames())
            self.cam.stop_video_capture()
        except Exception:  # noqa: BLE001
            pass

    def _grab_video_frame(self) -> np.ndarray:
        timeout_ms = int(self.v["exposure_ms"] * 2 + 500)
        a = self.cam.capture_video_frame(buffer_=self._buf, timeout=timeout_ms)
        try:
            self._dropped = int(self.cam.get_dropped_frames())
        except Exception:  # noqa: BLE001
            pass
        return self._fix(a)

    def _snapshot(self, abort: threading.Event) -> np.ndarray:
        return self._fix(self.cam.capture(buffer_=self._buf))

    def _fix(self, a: np.ndarray) -> np.ndarray:
        if a.ndim == 3:
            a = a[..., ::-1]  # the SDK's RGB24 is BGR
        return a
