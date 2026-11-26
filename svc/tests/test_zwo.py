# SPDX-License-Identifier: GPL-3.0-or-later
"""ZWO camera: FITS writing, the ASI224MC simulator (ROI, binning, formats, frame rate, video,
capture), and the camera service with it over Zenoh."""
import threading
import time

import numpy as np
import pytest

from maxmount_svc import zwo
from maxmount_svc.camera import CameraError, CameraService
from maxmount_svc.mount import MountClient
from maxmount_svc.proto import mpb

from .test_camera import Watch, cmd
from .test_e2e_mount_rt import served  # noqa: F401 - fixture


def read_fits(b: bytes):
    """Header dict and data of a FITS file written by fits_bytes."""
    hdr, i = {}, 0
    while True:
        card = b[i:i + 80].decode("ascii")
        i += 80
        if card.startswith("END"):
            break
        if "=" in card[:10]:
            k, v = card[:8].strip(), card[10:].split(" / ")[0].strip()
            hdr[k] = v.strip("'").strip() if v.startswith("'") else (v == "T" if v in ("T", "F") else float(v))
    i = (i + 2879) // 2880 * 2880
    shape = [int(hdr[f"NAXIS{n}"]) for n in range(int(hdr["NAXIS"]), 0, -1)]
    if hdr["BITPIX"] == 8:
        data = np.frombuffer(b, ">u1", int(np.prod(shape)), i).reshape(shape)
    else:
        data = (np.frombuffer(b, ">i2", int(np.prod(shape)), i).astype(np.int32) + int(hdr.get("BZERO", 0))).reshape(shape)
    return hdr, data


def test_fits_round_trip():
    a16 = (np.arange(48, dtype=np.uint16).reshape(6, 8) * 1300)
    h, d = read_fits(zwo.fits_bytes(a16, {"EXPTIME": 0.01, "INSTRUME": "test", "BAYERPAT": "RGGB"}))
    assert h["BITPIX"] == 16 and h["NAXIS1"] == 8 and h["NAXIS2"] == 6 and h["BZERO"] == 32768 and h["ROWORDER"] == "TOP-DOWN"
    assert h["EXPTIME"] == 0.01 and h["INSTRUME"] == "test" and h["BAYERPAT"] == "RGGB"
    assert np.array_equal(d, a16)
    rgb = np.random.default_rng(1).integers(0, 255, (4, 5, 3), dtype=np.uint8)
    h, d = read_fits(zwo.fits_bytes(rgb, {}))
    assert h["NAXIS"] == 3 and h["NAXIS3"] == 3 and np.array_equal(np.moveaxis(d, 0, -1), rgb)
    assert len(zwo.fits_bytes(rgb, {})) % 2880 == 0


def test_superpixel():
    raw = np.zeros((4, 4), np.uint16)
    raw[0::2, 0::2], raw[0::2, 1::2], raw[1::2, 0::2], raw[1::2, 1::2] = 100, 200, 400, 50
    rgb = zwo.superpixel_rgb(raw)
    assert rgb.shape == (2, 2, 3) and rgb[0, 0].tolist() == [100, 300, 50]


def test_simulator_geometry_formats_and_validation():
    b = zwo.ZwoSimBackend()
    b.open()
    for roi, want in (("full", (0, 0, 1304, 976)), ("640x480", (332, 248, 640, 480)), ("64x64", (620, 456, 64, 64))):
        b.set("roi", roi)
        assert b.roi() == want and b._frame().shape == want[3:1:-1]
    b.set("roi", "640x480")
    b.set("image_type", "RAW16")
    assert b._frame().dtype == np.uint16 and b.full_scale() == 65535
    b.set("image_type", "RGB24")
    assert b._frame().shape == (480, 640, 3)
    b.set("image_type", "RAW8")
    b.set("bin", "2")
    x, y, w, h = b.roi()
    assert (w, h) == (640, 480) and w % 8 == 0 and x % 2 == 0 and b._frame().shape == (480, 640)
    b.set("bin", "1")
    for name, value in (("gain", "9999"), ("gain", "abc"), ("roi", "333x7"), ("image_type", "RAW12"), ("bin", "3")):
        with pytest.raises(CameraError):
            b.set(name, value)
    s = {d["name"]: d for d in b.settings()}
    assert s["gain"]["numeric"] and s["gain"]["max"] == 510 and s["exposure_ms"]["unit"] == "ms" and "full" in s["roi"]["choices"]
    # Frame rate: exposure-limited, or USB-limited for big frames.
    b.set("exposure_ms", "100")
    assert b._interval() == pytest.approx(0.1)
    b.set("exposure_ms", "0.1")
    b.set("roi", "full")
    b.set("image_type", "RAW16")
    b.set("bandwidth", "40")
    assert b._interval() == pytest.approx(1304 * 976 * 2 / (2e8 * 0.4))
    b.set("bandwidth", "80")
    # More gain, more signal.
    b.set("image_type", "RAW8")
    b.set("roi", "128x128")
    b.set("exposure_ms", "5")
    b.set("gain", "0")
    low = b._frame().mean()
    b.set("gain", "300")
    assert b._frame().mean() > 2 * low


def test_simulator_video_and_capture():
    b = zwo.ZwoSimBackend()
    b.open()
    with pytest.raises(CameraError):
        b.preview_array()  # live view is off
    shot = b.capture(0, threading.Event(), True)
    assert shot.note == "single exposure" and shot.files[0][0].endswith(".fits")
    b.set("exposure_ms", "5")
    b.set_liveview(True)
    try:
        time.sleep(1.5)
        st = b.extra_status()
        assert 10 < st["fps"] <= 61 and st["roi"] == [332, 248, 640, 480] and st["sensor_width"] == 1304
        img, full = b.preview_array()
        assert img.shape == (240, 320, 3) and full == 255  # superpixel debayer
        b.set("roi", "320x240")  # geometry changes restart the stream
        img, _ = b.preview_array()
        assert img.shape == (120, 160, 3)
        b.set("image_type", "RAW16")
        shot = b.capture(0, threading.Event(), True)
        h, d = read_fits(shot.files[0][1])
        assert shot.note == "from the video stream" and shot.bit_depth == 16 and d.shape == (240, 320)
        assert h["EXPTIME"] == 0.005 and h["GAIN"] == 200 and h["XORGSUBF"] == 492 and h["BAYERPAT"] == "RGGB" and "CCD-TEMP" in h
    finally:
        b.set_liveview(False)
    assert b.extra_status()["fps"] == 0


def test_zwo_service_end_to_end(served, tmp_path):  # noqa: F811
    _client, settings = served
    with MountClient(settings.zenoh) as c, MountClient(settings.zenoh) as gui:
        svc = CameraService(c, zwo.ZwoSimBackend(), "zwo", tmp_path, liveview_fps=10)
        svc.start()
        status = Watch(gui, "camera/zwo/status", mpb.CameraStatus)
        frames = Watch(gui, "camera/zwo/frame", mpb.CameraFrame)
        try:
            st = status.wait(lambda s: s.connected)
            assert st.model.startswith("ZWO ASI224MC") and list(st.storage) == ["pi"] and st.save_to == "pi"
            assert st.sensor_width == 1304 and st.temperature_c == pytest.approx(24.5)
            gain = next(s for s in st.settings if s.name == "gain")
            assert gain.numeric and gain.max == 510
            assert not cmd(gui, "zwo", save_to="card").ok  # no card on a ZWO
            assert cmd(gui, "zwo", set=mpb.SetCameraSetting(name="exposure_ms", value="4")).ok
            assert not cmd(gui, "zwo", set=mpb.SetCameraSetting(name="gain", value="600")).ok
            assert cmd(gui, "zwo", liveview=True).ok
            st = status.wait(lambda s: s.liveview and s.fps > 10)  # (the Pi computes simulated frames more slowly)
            assert list(st.roi) == [332, 248, 640, 480]
            frames.wait(lambda f: f.kind == "liveview" and f.width == 320)
            assert cmd(gui, "zwo", set=mpb.SetCameraSetting(name="image_type", value="RAW16")).ok
            assert cmd(gui, "zwo", capture=mpb.Capture()).ok
            f = frames.wait(lambda f: f.kind == "capture")
            assert f.bit_depth == 16 and f.exposure_s == pytest.approx(0.004) and f.gain == 200
            fits = [p for p in f.files if p.endswith(".fits")]
            assert len(fits) == 1 and fits[0].startswith(str(tmp_path / "zwo"))
            status.wait(lambda s: s.captures == 1 and "from the video stream" in s.message)
            assert cmd(gui, "zwo", liveview=False).ok
            status.wait(lambda s: not s.liveview and s.fps == 0)
        finally:
            status.sub.undeclare()
            frames.sub.undeclare()
            svc.close()


def test_sdk_backend_reports_a_missing_library(tmp_path):
    pytest.importorskip("zwoasi")
    b = zwo.ZwoSdkBackend(str(tmp_path / "libASICamera2.so"))
    with pytest.raises(CameraError, match="ASI SDK|cannot load"):
        b.open()
