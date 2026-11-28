# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end in simulation: mount-rt (sim), mount-svc and the widefield camera simulator,
whose stars are drawn where the mount REALLY points (cameras.widefield.sim_model). A pointing
run must find that model; once applied, a solve shows almost no pointing error."""
import math
import time

import pytest

from maxmount_svc import pointfit
from maxmount_svc.mount import MountClient
from maxmount_svc.proto import mpb
from maxmount_svc.service import MountService
from maxmount_svc.widefield import WidefieldService, WidefieldSimBackend

from .test_e2e_mount_rt import served  # noqa: F401 - fixture


def _camera_ready(wf, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if wf.status().connected:
            return
        time.sleep(0.1)
    raise AssertionError("widefield camera never connected")


def _solve(ctl, sid, wf, timeout=30):
    out = ctl.query("camera/widefield/cmd", mpb.CameraCommand(solve=mpb.Solve(id=sid)).SerializeToString(), timeout=5)
    rep = mpb.CameraReply()
    rep.ParseFromString(out)
    assert rep.ok, rep.message
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = wf.status()
        if st.solution.id == sid:
            return st.solution
        time.sleep(0.1)
    raise AssertionError("no solution")


@pytest.fixture
def stack(served, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    client, settings = served
    settings.pointing_run.settle_s = 0.3
    settings.widefield.exposure_ms = 200
    settings.widefield.data_dir = str(tmp_path / "data")
    svc = MountService(settings, client)
    svc.start()
    cam_client = MountClient(settings.zenoh)
    backend = WidefieldSimBackend(settings.widefield, settings.site)
    wf = WidefieldService(cam_client, backend, settings)
    backend.pointing = wf.pointing
    wf.start()
    try:
        _camera_ready(wf)
        yield svc, wf, settings, cam_client
    finally:
        wf.close()
        backend.close()
        svc.close()
        cam_client.close()


def test_pointing_run_finds_the_simulated_model_and_applying_it_removes_the_error(stack):
    svc, wf, settings, ctl = stack
    truth = settings.widefield.sim_model
    r = svc.pointing.handle(mpb.PointingRequest(start=mpb.PointingRunStart(mode="full", points=12)))
    assert r.ok, r.message
    end = time.monotonic() + 300
    while svc.pointing.running and time.monotonic() < end:
        time.sleep(0.5)
    st = svc.pointing.status()
    assert st.state == "done", st.message
    solved = [p for p in st.points if p.state == "solved"]
    assert len(solved) >= 10, [(p.state, p.message) for p in st.points]
    assert any(p.flipped for p in solved)
    fit = {t.name: t for t in st.fit.terms}
    for name, v in truth.items():
        assert fit[name].value_arcsec == pytest.approx(v, abs=12), (name, {k: round(t.value_arcsec, 1) for k, t in fit.items()})
    assert st.fit.rms_before_arcsec > 200 and st.fit.rms_arcsec < 5, st.message
    # Every solve was logged as a candidate point.
    assert (wf.data_dir / "solves.jsonl").read_text().count("\n") >= len(solved)

    r = svc.pointing.handle(mpb.PointingRequest(apply=True))
    assert r.ok and r.status.applied and r.status.current.version, r.message
    assert pointfit.model_path(settings).exists() and len(r.status.history) == 1
    # A new target uses the model: the widefield now points where the mount believes.
    rep = svc.handle(mpb.TargetCommand(goto_altaz=mpb.AltAz(az_deg=200, el_deg=50, hold_s=0)))
    assert rep.accepted, rep.message
    end = time.monotonic() + 90
    while svc.status().state != mpb.SERVICE_STATE_TRACKING and time.monotonic() < end:
        time.sleep(0.2)
    time.sleep(1.0)
    sol = _solve(ctl, "after", wf)
    assert sol.ok, sol.message
    assert math.hypot(sol.err_xel_arcsec, sol.err_el_arcsec) < 10, sol.message
    assert sol.el_deg == pytest.approx(50, abs=0.05)
