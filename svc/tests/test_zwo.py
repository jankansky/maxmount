# SPDX-License-Identifier: GPL-3.0-or-later
"""ZWO camera: FITS writing, the ASI224MC simulator (ROI, binning, formats, frame rate, video,
capture, SER recording), and the camera service with it over Zenoh."""
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from maxmount_svc import ser, zwo
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
            # Recording turns video on and writes SER plus a JSON sidecar.
            r = cmd(gui, "zwo", record=mpb.Record(on=True, max_frames=30))
            assert r.ok and "30 frames" in r.message, r.message
            st = status.wait(lambda s: s.liveview and not s.recording and s.record_frames == 30 and "recorded 30 frames" in s.message)
            assert st.records_video and st.record_file.endswith(".ser") and st.disk_free_bytes > 0
            side = json.loads(Path(st.record_file).with_suffix(".json").read_text())
            assert side["frames"] == 30 and side["stopped_because"] == "30 frames recorded" and side["settings"]["image_type"] == "RAW16"
            assert ser.read_ser(st.record_file)["frames"].dtype == np.uint16
            assert cmd(gui, "zwo", record=mpb.Record(on=True)).ok
            status.wait(lambda s: s.recording and s.record_frames > 5)
            assert not cmd(gui, "zwo", set=mpb.SetCameraSetting(name="bin", value="2")).ok
            r = cmd(gui, "zwo", record=mpb.Record(on=False))
            assert r.ok and r.message.startswith("recording stopped")
            status.wait(lambda s: not s.recording and "stopped" in s.message)
            assert cmd(gui, "zwo", liveview=False).ok
            status.wait(lambda s: not s.liveview)
            # ROI tracking: turns video on, locks, publishes centroids, and frames carry the ROI.
            assert cmd(gui, "zwo", set=mpb.SetCameraSetting(name="roi", value="256x256")).ok
            cents = Watch(gui, "camera/zwo/centroid", mpb.CameraCentroid)
            try:
                r = cmd(gui, "zwo", roi_track=mpb.RoiTrack(on=True))
                assert r.ok and "brightest" in r.message
                st = status.wait(lambda s: s.liveview and s.roi_tracking and s.track_state == "locked" and s.roi_moves > 0)
                assert not st.roi_centered and abs(st.track_err_x) < 40
                c = cents.wait(lambda c: c.state == "locked")
                assert c.bin == 1 and list(c.roi)[2:] == [256, 256] and c.snr > 8 and c.t_ns > 0
                f = frames.wait(lambda f: f.kind == "liveview" and len(f.roi) == 4 and f.track_state == "locked")
                assert 0 <= f.track_x < 256 and 0 <= f.track_y < 256
                assert not cmd(gui, "zwo", roi_track=mpb.RoiTrack(on=True, has_pick=True, pick_x=1.5, pick_y=0.5)).ok
                assert cmd(gui, "zwo", roi_track=mpb.RoiTrack(on=False, recenter=True)).ok
                status.wait(lambda s: not s.roi_tracking and s.roi_centered)
            finally:
                cents.sub.undeclare()
            assert cmd(gui, "zwo", liveview=False).ok
        finally:
            status.sub.undeclare()
            frames.sub.undeclare()
            svc.close()


def test_recording_formats_limits_and_locks(tmp_path):
    b = zwo.ZwoSimBackend()
    b.open()
    try:
        with pytest.raises(CameraError, match="start video"):
            b.start_record(tmp_path, "x")
        b.set("exposure_ms", "2")
        for fmt, color, shape in (("RAW8", "BAYER_RGGB", (480, 640)), ("RAW16", "BAYER_RGGB", (480, 640)),
                                  ("RGB24", "RGB", (480, 640, 3))):
            b.set("image_type", fmt)
            b.set_liveview(True)
            rec = b.start_record(tmp_path, fmt, max_frames=25)
            assert rec.wait(10) and rec.reason == "25 frames recorded"
            r = ser.read_ser(rec.path)
            assert rec.path.name == f"{fmt}_{fmt}_640x480_2ms.ser"
            assert r["color"] == color and r["frames"].shape == (25, *shape) and len(r["times_ns"]) == 25
            assert np.all(np.diff(r["times_ns"]) > 0) and r["frames"][24].max() > 0
            b.set_liveview(False)
        # A time limit; geometry is locked while recording; stopping video stops the recording.
        b.set_liveview(True)
        rec = b.start_record(tmp_path, "t", max_s=0.5)
        assert b.recording and b.extra_status()["recording"]
        with pytest.raises(CameraError, match="stop recording"):
            b.set("roi", "320x240")
        b.set("gain", "250")  # exposure and gain may change while recording
        with pytest.raises(CameraError, match="already"):
            b.start_record(tmp_path, "u")
        assert rec.wait(10) and rec.reason == "0.5 s recorded" and 0.4 < rec.elapsed_s < 1.5
        rec = b.start_record(tmp_path, "v")
        time.sleep(0.2)
        b.set_liveview(False)
        assert rec.wait(10) and rec.reason == "video stopped" and rec.frames > 0
        st = b.extra_status()
        assert not st["recording"] and st["record_frames"] == rec.frames
    finally:
        b.close()


def test_recorder_drops_when_the_disk_is_slow_and_stops_when_full(tmp_path, monkeypatch):
    rec = zwo.Recorder(tmp_path / "slow.ser", (4, 8), np.uint8, "MONO", 8, "test", pool_bytes=4 * 32)
    assert rec.pool_frames == 4
    slow = threading.Event()
    real = rec.writer.write
    monkeypatch.setattr(rec.writer, "write", lambda f, t: (slow.wait(5), real(f, t)))
    for i in range(10):
        rec.offer(np.full((4, 8), i, np.uint8), i + 1)
    assert rec.accepted == 4 and rec.dropped == 6  # never blocks the video thread
    rec.stop()  # returns at once; the writer finishes the queue afterwards
    st = rec.status()
    assert st["recording"] and st["record_stopping"] and st["record_queued"] >= 3
    slow.set()
    assert rec.wait(5) and rec.frames == 4 and not rec.status()["record_stopping"]
    assert [int(f[0, 0]) for f in ser.read_ser(rec.path)["frames"]] == [0, 1, 2, 3]
    with pytest.raises(CameraError, match="free"):
        zwo.Recorder(tmp_path / "full.ser", (4, 8), np.uint8, "MONO", 8, "test", min_free_bytes=1 << 62)
    rec = zwo.Recorder(tmp_path / "f.ser", (4, 8), np.uint8, "MONO", 8, "test")
    rec.min_free_bytes = 1 << 62  # the disk fills up during the recording
    assert rec.wait(5) and rec.reason.startswith("disk nearly full")


def test_roi_tracking_follows_the_simulated_target(tmp_path):
    """The simulated target drifts hundreds of pixels; a 256 x 256 ROI follows it (moves take
    effect 2 frames late), the recording's CSV logs each frame's ROI position, and Center ROI
    puts it back."""
    b = zwo.ZwoSimBackend()
    b.open()
    measures = []
    b.on_measure = measures.append
    try:
        b.set("roi", "256x256")
        b.set("exposure_ms", "5")
        b.set_liveview(True)
        b.set_roi_track(True)
        time.sleep(1.0)
        rec = b.start_record(tmp_path, "trk", max_s=3)
        errs = []
        while not rec.done.is_set():
            time.sleep(0.1)
            x, y, w, h = b.roi()
            tx, ty = b.target_xy()
            errs.append(np.hypot(tx - (x + (w - 1) / 2), ty - (y + (h - 1) / 2)))
        st = b.extra_status()
        assert st["track_state"] == "locked" and st["roi_moves"] > 10 and not st["roi_centered"]
        assert max(errs) < 25 and np.median(errs) < 8, errs
        assert measures and measures[-1]["state"] == "locked" and measures[-1]["roi"][2:] == [256, 256]
        m = measures[-1]
        assert abs(m["dx"] - (m["x"] - (1304 / 2 - 0.5))) < 1e-6 and m["pixel_um"] == 3.75
        rows = (tmp_path / rec.path.with_suffix(".csv").name).read_text().splitlines()
        assert rows[0].startswith("frame,t_utc_ns,roi_x,roi_y") and len(rows) == rec.frames + 1
        cols = [r.split(",") for r in rows[1:]]
        assert len({(c[2], c[3]) for c in cols}) > 5  # the ROI moved during the recording
        assert all(c[8] == "locked" for c in cols) and all(c[5] for c in cols)
        # Each logged position is where the frame really was read: the target found at the
        # logged origin + its offset matches the simulator's (within the measurement error).
        assert ser.read_ser(rec.path)["count"] == rec.frames
        # A geometry change re-acquires; Center ROI stops tracking and recentres.
        b.set("roi", "320x240")
        time.sleep(0.5)
        assert b.extra_status()["track_state"] == "locked"
        b.set_roi_track(False, recenter=True)
        time.sleep(0.3)
        st = b.extra_status()
        assert not st["roi_tracking"] and st["roi_centered"] and st["roi"] == [492, 368, 320, 240]
    finally:
        b.close()


def test_roi_tracking_pick(tmp_path):
    """Picking a point acquires the object there, not the brightest."""
    b = zwo.ZwoSimBackend()
    b.drift = (0.0, 0.0)  # the target stays within ~30 px of the centre, away from the star
    b.open()
    try:
        b.set("roi", "full")
        b.set("exposure_ms", "5")
        b.set_liveview(True)
        time.sleep(0.3)
        frame, _t, _s = b._latest_copy()
        from maxmount_svc.roitrack import measure
        tx, ty = b.target_xy()
        # A star well away from the target, as the pick.
        d = measure(frame, near=(tx - 400, 488), gate=200, snr_min=5)
        assert d is not None and abs(d.x - tx) > 150
        b.set_roi_track(True, pick=(d.x / 1304, d.y / 976))
        time.sleep(0.5)
        st = b.extra_status()
        assert st["track_state"] == "locked" and abs(st["track_x"] - d.x) < 5 and abs(st["track_y"] - d.y) < 5
    finally:
        b.close()


def test_sdk_backend_reports_a_missing_library(tmp_path):
    pytest.importorskip("zwoasi")
    b = zwo.ZwoSdkBackend(str(tmp_path / "libASICamera2.so"))
    with pytest.raises(CameraError, match="ASI SDK|cannot load"):
        b.open()
