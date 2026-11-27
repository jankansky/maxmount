# SPDX-License-Identifier: GPL-3.0-or-later
"""The camera loop offline: geometry, calibration, and the loop correcting a satellite's TLE
error through a real pass (the "truth" from simsky.project, the corrections from the plan's
Jacobian, so a sign error anywhere shows as a loop that does not converge)."""
import dataclasses
import math

import numpy as np
import pytest

from maxmount_svc import closedloop, planner
from maxmount_svc.closedloop import ARCSEC, CameraGeometry
from maxmount_svc.simsky import project
from maxmount_svc.targets import FixedTarget, SatelliteTarget

from .conftest import ISS, NS

DEG = math.pi / 180


def test_geometry_round_trip_and_describe():
    for rot, flip in ((0, False), (20, False), (-135, True), (90, True)):
        g = CameraGeometry.from_config(0.38, rot, flip)
        d = g.describe()
        assert d["arcsec_per_px"] == pytest.approx(0.38, abs=1e-4)
        assert d["rotation_deg"] == pytest.approx(rot, abs=0.01) and d["flip"] == flip
        p = np.array([12.0, -7.5])
        assert np.allclose(g.to_px(g.to_axis(*p)), p)


def test_save_and_load(tmp_path):
    g = CameraGeometry.from_config(0.5, 33, True)
    g.source = "calibrated now"
    path = CameraGeometry.state_path("sys", "zwo", tmp_path)
    g.save(path)
    h = CameraGeometry.load(path)
    assert np.allclose(h.m, g.m) and h.source == "calibrated now"
    path.write_text("not json")
    assert CameraGeometry.load(path) is None


def test_calibration_recovers_the_geometry():
    truth = CameraGeometry.from_config(0.38, 20, False)
    drift = np.array([3.0, -2.0])  # px/s: the target drifts while uncorrected
    start = np.array([15.0, 40.0])

    def seen(t, q):  # the target's position when the boresight has moved by q
        return start + drift * t - truth.to_px(q)

    q1, q2 = np.array([20 * ARCSEC, 0]), np.array([0, 20 * ARCSEC])
    g = closedloop.calibrate(seen(0, 0 * q1), 0, seen(1.5, q1), 1.5, seen(3, q2), 3, seen(4.5, 0 * q1), 4.5, q1, q2, "cal")
    assert np.allclose(g.m, truth.m, rtol=1e-9)
    with pytest.raises(ValueError):
        closedloop.calibrate(start, 0, start, 1, start, 2, start, 3, q1, q2, "cal")


def _iss_pass(site, limits, tracking, mode=None):
    target = SatelliteTarget(site, *ISS, name="ISS")
    t0 = int((target.epoch_ns // NS) * NS)
    for p in planner.find_passes(target, t0, t0 + 2 * 86400 * NS, tracking.horizon_min_el_deg * DEG):
        if p.max_el > 30 * DEG:
            modes = (mode,) if mode else ("normal", "flipped")
            best = planner.choose_plan([planner.plan_mode(m, target, p.rise_ns, p.set_ns, *limits, tracking, site)
                                        for m in modes])
            if best is not None:
                return target, best, p
    pytest.skip("no suitable ISS pass")


@pytest.mark.parametrize("rot_err_deg,flip,mode", [(0, False, "normal"), (25, False, "normal"), (-25, True, "flipped")])
def test_loop_finds_the_tle_error_through_a_pass(site, limits, tracking, rot_err_deg, flip, mode):
    """The mount points where the plan says (with the loop's corrections); the target is
    where the "true" TLE (0.12 s late, 25" to the left) puts it. The loop's geometry may be
    off by some rotation, as before a calibration."""
    target, plan, p = _iss_pass(site, limits, tracking, mode)
    assert plan.mode == mode
    truth_geom = CameraGeometry.from_config(0.38, 20, flip)
    loop_geom = CameraGeometry.from_config(0.38, 20 + rot_err_deg, flip)
    truth = dataclasses.replace(target, time_offset_s=0.12, cross_track_rad=25 * ARCSEC)
    errs = []
    t = p.rise_ns + 30 * NS
    for _ in range(25):
        a, e = (float(v[0]) for v in plan.mech(np.int64(t)))
        px = project(truth.observed(np.int64(t)), a, e, truth_geom)
        assert px is not None
        q = loop_geom.to_axis(*px)
        errs.append(float(np.hypot(*q)) / ARCSEC)
        j, names, _ = closedloop.jacobian(plan, target, "tle", t)
        assert names == ("time_offset_s", "cross_track_rad")
        x = 0.5 * closedloop.correction(j, names, q, 120 * ARCSEC)
        target.time_offset_s += x[0]
        target.cross_track_rad += x[1]
        t += int(1.2 * NS)
    assert errs[0] > 30  # it started well off
    assert errs[-1] < 1.0, errs
    assert target.time_offset_s == pytest.approx(0.12, abs=0.005)
    assert target.cross_track_rad / ARCSEC == pytest.approx(25, abs=2)


def test_loop_over_the_top_corrects_time_and_azimuth(site, limits, tracking):
    # The test passes are all far from the zenith: allow a large over-the-top error.
    target, plan, p = _iss_pass(site, limits, dataclasses.replace(tracking, over_the_top_tolerance_deg=20), "over_the_top")
    geom = CameraGeometry.from_config(0.38, 0, False)
    truth = dataclasses.replace(target, time_offset_s=0.12)
    d_az, errs = 0.0, []
    t = p.rise_ns + 30 * NS
    for _ in range(20):
        a, e = (float(v[0]) for v in plan.mech(np.int64(t)))
        a_true, e_true = (float(v[0]) for v in plan.mech_with(truth, np.int64(t)))
        # Over the top the plan itself is off the target by up to the tolerance; the camera
        # sees the error between where the over-the-top plan points with the true TLE and now.
        q = np.array([(a_true - a - d_az) * math.cos(e), e_true - e])
        errs.append(float(np.hypot(*q)) / ARCSEC)
        j, names, _ = closedloop.jacobian(plan, target, "tle", t)
        assert names == ("time_offset_s", "az_rad")
        x = 0.5 * closedloop.correction(j, names, q, 120 * ARCSEC)
        target.time_offset_s += x[0]
        d_az += x[1]
        t += int(1.2 * NS)
    assert errs[-1] < 0.05 * errs[0]
    assert target.time_offset_s == pytest.approx(0.12, abs=0.01)


def test_loop_fixed_target_uses_axis_offsets(limits, tracking):
    """A fixed alt-az target with a pointing error: the loop finds az/el axis offsets."""
    target = FixedTarget(120 * DEG, 50 * DEG, name="fixed")
    t0 = 1_700_000_000 * NS
    plan = planner.plan_mode("normal", target, t0, t0 + 600 * NS, *limits, tracking, step_s=10.0)
    geom = CameraGeometry.from_config(0.38, -60, True)
    d_az, d_el = 0.0, 0.0
    true = (120 * DEG + 30 * ARCSEC / math.cos(50 * DEG), 50 * DEG - 18 * ARCSEC)
    for k in range(20):
        t = t0 + k * NS
        a, e = (float(v[0]) for v in plan.mech(np.int64(t)))
        q = geom.to_axis(*project(true, a + d_az, e + d_el, geom))
        j, names, _ = closedloop.jacobian(plan, target, "altaz", t)
        assert names == ("az_rad", "el_rad")
        x = 0.5 * closedloop.correction(j, names, q, 120 * ARCSEC)
        d_az += x[0]
        d_el += x[1]
    assert d_az * math.cos(50 * DEG) / ARCSEC == pytest.approx(30, abs=0.1)
    assert d_el / ARCSEC == pytest.approx(-18, abs=0.1)


def test_controller_waits_for_settled_centroids_and_detects_divergence():
    c = closedloop.LoopController(closedloop.LoopSettings(avg_s=0.25, settle_s=0.6))
    c.reset(0)
    for k in range(10):  # 50 Hz for 0.2 s: not enough yet
        c.add(closedloop.Measurement(k * 20_000_000, np.array([4.0, 2.0]), 20.0))
    assert c.average(200_000_000) is None
    for k in range(10, 16):
        c.add(closedloop.Measurement(k * 20_000_000, np.array([4.0, 2.0]), 20.0))
    p, t = c.average(320_000_000)
    assert np.allclose(p, [4, 2])
    # Scattered centroids (the mount still arriving): no average.
    c2 = closedloop.LoopController()
    for k in range(20):
        c2.add(closedloop.Measurement(k * 20_000_000, np.array([4.0 + 10 * k, 2.0]), 20.0))
    assert c2.average(400_000_000) is None
    c.applied(320_000_000)
    c.add(closedloop.Measurement(500_000_000, np.array([1.0, 1.0]), 20.0))  # before the correction landed
    assert not c.buf
    c.add(closedloop.Measurement(1_000_000_000, np.array([1.0, 1.0]), 3.0))  # too faint
    assert not c.buf
    # Divergence: the error keeps growing.
    c = closedloop.LoopController()
    grew = [c.diverging(e * ARCSEC) or c.applied(0) for e in (30, 40, 50, 70, 90)]
    assert c.diverging(120 * ARCSEC) or any(grew)


def test_solve_drops_an_unobservable_direction():
    j = np.array([[1e-6, 0.0], [0.0, 1.0]])  # near the zenith: the azimuth axis hardly moves the view
    x = closedloop.solve(j, np.array([1e-5, 2e-5]))
    assert x[0] == 0.0 and x[1] == pytest.approx(2e-5)
