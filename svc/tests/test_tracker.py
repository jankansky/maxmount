# SPDX-License-Identifier: GPL-3.0-or-later
import numpy as np

from maxmount_svc import planner, tracker

from .conftest import DEG, NS, GreatCircleTarget

T0 = 1_800_000_000 * NS


def test_move_profile_matches_mount_rt():
    m = tracker.MoveProfile(1.0, -2.0, 0.5, 0.25)  # same case as rt/tests/test_move_profile.cpp
    assert abs(m.duration - 8.0) < 1e-12
    assert m.at(0) == (1.0, 0.0)
    p, v = m.at(m.duration)
    assert abs(p + 2.0) < 1e-12 and abs(v) < 1e-12


def test_points_have_consistent_velocities(limits, tracking):
    az, el = limits
    target = GreatCircleTarget(T0, a0=10 * DEG, d=30 * DEG, rate=1.0 * DEG)
    plan = planner.plan_mode("normal", target, T0, T0 + 60 * NS, az, el, tracking)
    pts = tracker.plan_points(plan, T0 + NS, T0 + 11 * NS, 50_000_000)
    for axis in ("az", "el"):
        t = np.array([p[0] for p in pts[axis]])
        x = np.array([p[1] for p in pts[axis]])
        v = np.array([p[2] for p in pts[axis]])
        assert np.all(np.diff(t) > 0)
        assert np.allclose(np.gradient(x, t / NS)[1:-1], v[1:-1], atol=1e-5)


def test_slew_joins_the_moving_target(limits, tracking):
    az, el = limits
    target = GreatCircleTarget(T0, a0=10 * DEG, d=30 * DEG, rate=1.0 * DEG)
    plan = planner.plan_mode("normal", target, T0, T0 + 300 * NS, az, el, tracking)
    slew = tracker.plan_slew(plan, {"az": 0.0, "el": 0.5}, now_ns=T0, tracking=tracking)
    pts = slew.points(50_000_000)
    a_join, e_join = plan.mech(slew.t_join_ns)
    assert pts["az"][-1][0] == slew.t_join_ns
    assert abs(pts["az"][-1][1] - a_join[0]) < 1e-9 and abs(pts["el"][-1][1] - e_join[0]) < 1e-9
    assert max(p.duration for p in slew.profiles.values()) * NS <= slew.t_join_ns - slew.t0_ns


def test_points_end_exactly_at_the_requested_time(limits, tracking):
    az, el = limits
    target = GreatCircleTarget(T0, a0=10 * DEG, d=30 * DEG, rate=1.0 * DEG)
    plan = planner.plan_mode("normal", target, T0, T0 + 60 * NS, az, el, tracking)
    pts = tracker.plan_points(plan, T0, T0 + 1_030_000_000, 50_000_000)
    assert pts["az"][-1][0] == T0 + 1_030_000_000
    assert pts["az"][-2][0] == T0 + 1_000_000_000
