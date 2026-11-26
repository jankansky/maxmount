# SPDX-License-Identifier: GPL-3.0-or-later
"""The stills-camera service (mount-svc dslr) with the simulated camera: settings, capture to the
Pi with a sidecar, live view, bulb and abort, over Zenoh like the GUI gateway uses it."""
import io
import json
import threading
import time

import numpy as np
import pytest
from PIL import Image

from maxmount_svc import camera
from maxmount_svc.mount import MountClient
from maxmount_svc.proto import mpb

from .test_e2e_mount_rt import served  # noqa: F401 - fixture


def test_eval_shutter():
    assert camera.eval_shutter("1/250") == pytest.approx(0.004)
    assert camera.eval_shutter("2") == 2 and camera.eval_shutter('30"') == 30 and camera.eval_shutter("2.5") == 2.5
    assert camera.eval_shutter("Bulb") == 0


def test_frame_stats_and_stretch():
    a = np.full((200, 300), 20, np.uint8)
    a[:10, :10] = 255  # 100 saturated pixels of 60000
    buf = io.BytesIO()
    Image.fromarray(a).convert("RGB").save(buf, "JPEG", quality=95)
    f = camera.frame_from_jpeg(buf.getvalue(), "capture", stretch=False)
    assert (f.width, f.height) == (300, 200) and len(f.histogram) == 64 and sum(f.histogram) == 60000
    assert abs(f.median - 20) <= 2 and f.saturated_frac == pytest.approx(100 / 60000, rel=0.3)
    s = camera.frame_from_jpeg(buf.getvalue(), "capture", stretch=True)
    im = np.asarray(Image.open(io.BytesIO(s.jpeg)).convert("L"))
    assert s.stretched and im.max() > 200  # the stretch spans the range
    big = Image.new("RGB", (4000, 3000))
    buf = io.BytesIO()
    big.save(buf, "JPEG")
    assert max(camera.frame_from_jpeg(buf.getvalue(), "capture", False, max_px=1600).width, 0) <= 1600


def test_sim_backend():
    b = camera.SimBackend(time_scale=0.001)
    assert "simulated" in b.open() and b.battery() == 87
    names = [d["name"] for d in b.settings()]
    assert names == ["iso", "shutterspeed", "imagequality", "capturetarget"]
    with pytest.raises(camera.CameraError):
        b.set("iso", "123")
    b.set("imagequality", "NEF+Basic")
    shot = b.capture(0, threading.Event(), download_raw=False)
    assert [n.split(".")[1] for n, _ in shot.files] == ["JPG", "NEF"] and shot.files[1][1] is None
    assert shot.preview_jpeg[:2] == b"\xff\xd8" and len(shot.card_paths) == 2


class Watch:
    def __init__(self, client, key, msg_type):
        self.items, self.lock = [], threading.Lock()
        self.msg_type = msg_type
        self.sub = client.session.declare_subscriber(client.key(key), self._on)

    def _on(self, sample):
        m = self.msg_type()
        m.ParseFromString(sample.payload.to_bytes())
        with self.lock:
            self.items.append(m)

    def wait(self, pred, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for m in reversed(self.items):
                    if pred(m):
                        return m
            time.sleep(0.05)
        raise AssertionError(f"no message matched; last: {self.items[-1] if self.items else None}")


def cmd(gui, camera_id="dslr", **kw):
    out = gui.query(f"camera/{camera_id}/cmd", mpb.CameraCommand(**kw).SerializeToString(), timeout=20)
    assert out is not None, "no reply from the camera service"
    r = mpb.CameraReply()
    r.ParseFromString(out)
    return r


@pytest.fixture
def cam_service(served, tmp_path):  # noqa: F811
    _client, settings = served
    svc_client = MountClient(settings.zenoh)
    gui = MountClient(settings.zenoh)
    svc = camera.CameraService(svc_client, camera.SimBackend(time_scale=0.02), "dslr", tmp_path, liveview_fps=10)
    svc.start()
    try:
        yield svc, gui, tmp_path
    finally:
        svc.close()
        gui.close()
        svc_client.close()


def test_camera_service_end_to_end(cam_service):
    svc, gui, tmp = cam_service
    status = Watch(gui, "camera/dslr/status", mpb.CameraStatus)
    frames = Watch(gui, "camera/dslr/frame", mpb.CameraFrame)
    st = status.wait(lambda s: s.connected)
    assert st.model.startswith("Nikon D500") and st.simulated and st.battery_percent == 87 and st.bulb_supported
    assert {s.name for s in st.settings} == {"iso", "shutterspeed", "imagequality", "capturetarget"}

    r = cmd(gui, set=mpb.SetCameraSetting(name="iso", value="800"))
    assert r.ok, r.message
    status.wait(lambda s: any(x.name == "iso" and x.value == "800" for x in s.settings))
    r = cmd(gui, set=mpb.SetCameraSetting(name="iso", value="7"))
    assert not r.ok and "not offered" in r.message
    assert not cmd(gui, save_to="cloud").ok

    # Capture to the Pi: files, sidecar, a stretched preview with statistics.
    assert cmd(gui, save_to="pi").ok
    status.wait(lambda s: s.save_to == "pi" and any(x.name == "capturetarget" and "RAM" in x.value for x in s.settings))
    r = cmd(gui, capture=mpb.Capture())
    assert r.ok, r.message
    f = frames.wait(lambda m: m.kind == "capture")
    assert f.stretched and f.iso == "800" and f.exposure_s == 2 and len(f.histogram) == 64 and f.jpeg[:2] == b"\xff\xd8"
    saved = [p for p in f.files if not p.startswith("card:")]
    assert len(saved) == 2 and all(p.startswith(str(tmp)) for p in saved)
    side = json.loads(next((tmp / "dslr").rglob("*.json")).read_text())
    assert side["exposure_s"] == 2 and side["settings"]["iso"] == "800" and len(side["files"]) == 2
    st = status.wait(lambda s: s.captures == 1 and s.state == "idle")
    assert "capture 1" in st.message

    # A second capture while one runs is refused; bulb with abort ends early.
    assert cmd(gui, save_to="card").ok
    assert cmd(gui, set=mpb.SetCameraSetting(name="shutterspeed", value="Bulb")).ok
    assert cmd(gui, capture=mpb.Capture(bulb_s=600)).ok  # 12 s at time_scale 0.02
    status.wait(lambda s: s.state == "exposing" and s.exposure_s == 600)
    assert not cmd(gui, capture=mpb.Capture()).ok
    assert cmd(gui, abort=True).ok
    f = frames.wait(lambda m: m.kind == "capture" and m.exposure_s == 600, timeout=10)
    assert all(p.startswith("card:") for p in f.files)

    # Live view frames, then off.
    assert cmd(gui, liveview=True).ok
    frames.wait(lambda m: m.kind == "liveview" and m.width == 640)
    status.wait(lambda s: s.liveview and s.state == "liveview")
    assert cmd(gui, liveview=False).ok
    status.wait(lambda s: not s.liveview and s.state == "idle")
    assert cmd(gui, stretch=False).ok
    status.wait(lambda s: not s.stretch)
    status.sub.undeclare()
    frames.sub.undeclare()


def test_disconnected_camera_reports_and_refuses(served, tmp_path):  # noqa: F811
    _client, settings = served

    class Absent(camera.SimBackend):
        def open(self):
            raise camera.CameraError("no camera found over USB")

    with MountClient(settings.zenoh) as c, MountClient(settings.zenoh) as gui:
        svc = camera.CameraService(c, Absent(), "dslr", tmp_path)
        svc.start()
        try:
            status = Watch(gui, "camera/dslr/status", mpb.CameraStatus)
            st = status.wait(lambda s: s.state == "disconnected" and "no camera" in s.message)
            assert not st.connected
            r = cmd(gui, capture=mpb.Capture())
            assert not r.ok and "not connected" in r.message
            assert cmd(gui, save_to="both").ok  # remembered for when it connects
            status.sub.undeclare()
        finally:
            svc.close()
