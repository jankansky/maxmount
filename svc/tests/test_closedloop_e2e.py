# SPDX-License-Identifier: GPL-3.0-or-later
"""The camera loop end to end: mount-rt (simulated mount), mount-svc and the simulated ZWO
camera service over Zenoh. The simulated camera draws the target where it "really" is relative
to where the mount points (simsky: a 40", -25" pointing error and a sensor rotated 20 deg),
the camera's ROI tracking publishes centroids, and mount-svc's loop finds the offsets; then a
calibration measures the sensor's orientation."""
import math
import time

import pytest

from maxmount_svc import zwo
from maxmount_svc.camera import CameraService
from maxmount_svc.mount import MountClient
from maxmount_svc.proto import mpb
from maxmount_svc.service import MountService
from maxmount_svc.simsky import SimSky

from .test_camera import cmd
from .test_e2e_mount_rt import served  # noqa: F401 - fixture
from .test_service_e2e import StatusWatch, query


def target(gui, **kw):
    return query(gui, "svc/target", mpb.TargetCommand(**kw), mpb.TargetReply)


def test_camera_loop_corrects_pointing_and_calibrates(served, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))  # the calibration is saved here
    client, settings = served
    z = settings.zwo
    svc = MountService(settings, client)
    svc.start()
    with MountClient(settings.zenoh) as cam_client, MountClient(settings.zenoh) as gui:
        backend = zwo.ZwoSimBackend()
        sky = SimSky(settings, cam_client)
        backend.sky = sky.offset_px
        cam = CameraService(cam_client, backend, "zwo", tmp_path, liveview_fps=5)
        cam.start()
        watch = StatusWatch(gui)
        try:
            el = 45.0
            az0 = math.degrees(client.axis("az").cmd_position_rad) % 360
            r = target(gui, goto_altaz=mpb.AltAz(az_deg=az0 + 3, el_deg=el, name="fixed"))
            assert r.accepted, r.message
            watch.wait(lambda s: s.state == mpb.SERVICE_STATE_TRACKING, timeout=60)
            time.sleep(2)  # the axes catch up with the trajectory
            # Not before the camera has the target.
            r = target(gui, calibrate_camera=mpb.CalibrateCamera())
            assert not r.accepted and "centroid" in r.message
            assert cmd(gui, "zwo", set=mpb.SetCameraSetting(name="roi", value="640x480")).ok
            assert cmd(gui, "zwo", roi_track=mpb.RoiTrack(on=True)).ok
            r = target(gui, closed_loop=mpb.ClosedLoop(on=True))
            assert r.accepted, r.message
            st = watch.wait(lambda s: s.loop.updates >= 4 and s.loop.err_arcsec < 2 and s.loop.state != "waiting", timeout=60)
            assert st.loop.on and st.loop.corrects == "az, el" and st.loop.geometry == "config"
            # The offsets found are the simulated pointing error (on the sky: az x cos el).
            st = watch.wait(lambda s: abs(s.offsets.el_arcsec - z.sim_el_error_arcsec) < 3, timeout=30)
            assert st.offsets.az_arcsec * math.cos(math.radians(el)) == pytest.approx(z.sim_az_error_arcsec, abs=3)
            # Hand corrections add to the loop's, which then takes them out again.
            before = st.offsets.el_arcsec
            assert target(gui, add_offsets=mpb.Offsets(el_arcsec=30)).accepted
            watch.wait(lambda s: s.offsets.el_arcsec > before + 20, timeout=5)
            watch.wait(lambda s: abs(s.offsets.el_arcsec - z.sim_el_error_arcsec) < 3, timeout=30)

            # Calibration: measures the simulated sensor's rotation and scale, then the loop resumes.
            r = target(gui, calibrate_camera=mpb.CalibrateCamera())
            assert r.accepted, r.message
            st = watch.wait(lambda s: s.loop.geometry.startswith("calibrated") or "failed" in s.loop.note, timeout=60)
            assert st.loop.geometry.startswith("calibrated"), st.loop.note
            assert st.loop.rotation_deg == pytest.approx(z.sim_rotation_deg, abs=3)
            assert st.loop.arcsec_per_px == pytest.approx(z.sim_arcsec_per_px, rel=0.1)
            assert not st.loop.flip
            st = watch.wait(lambda s: s.loop.state in ("correcting", "settling") and s.loop.err_arcsec < 3, timeout=30)
            assert (tmp_path / "state" / "maxmount" / f"{settings.name}-camera-zwo.json").exists()
            # Back to the system file's geometry; loop off.
            assert target(gui, calibrate_camera=mpb.CalibrateCamera(reset=True)).accepted
            assert target(gui, closed_loop=mpb.ClosedLoop(on=False)).accepted
            st = watch.wait(lambda s: not s.loop.on and s.loop.geometry == "config")
        finally:
            watch.sub.undeclare()
            cam.close()
            backend.close()
            sky.close()
            svc.close()
