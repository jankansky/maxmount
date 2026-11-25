# SPDX-License-Identifier: GPL-3.0-or-later
import math

import numpy as np

from maxmount_svc import astro, planner
from maxmount_svc.pointing import PointingModel
from maxmount_svc.targets import FixedTarget

from .conftest import DEG, NS, GreatCircleTarget

T0 = 1_800_000_000 * NS


def test_near_zenith_pass_uses_over_the_top(limits, tracking):
    az, el = limits
    # 1 deg from the zenith, 1 deg/s, from 12 deg above one horizon to 12 deg above the other.
    target = GreatCircleTarget(T0, a0=40 * DEG, d=1.0 * DEG, rate=1.0 * DEG, start=12 * DEG)
    best, plans = planner.plan_all(target, T0, T0 + 156 * NS, az, el, tracking)
    by = {p.mode: p for p in plans}
    assert not by["normal"].feasible and "az rate" in by["normal"].problems[0]
    # The keyhole is intrinsic: the flipped branch needs exactly the same azimuth rate.
    assert math.isclose(by["flipped"].peak_vel[0], by["normal"].peak_vel[0], rel_tol=1e-6)
    ott = by["over_the_top"]
    assert ott.feasible, ott.problems
    assert best is ott
    assert abs(ott.max_error - 1.0 * DEG) < 0.02 * DEG
    assert np.ptp(ott.a) < 1e-9  # azimuth held
    assert ott.e.min() < 15 * DEG and ott.e.max() > 165 * DEG  # elevation goes over the top
    assert np.all(np.diff(ott.e) > 0)


def test_low_pass_tracks_exactly(limits, tracking):
    az, el = limits
    target = GreatCircleTarget(T0, a0=200 * DEG, d=50 * DEG, rate=0.8 * DEG, start=30 * DEG)
    best, _ = planner.plan_all(target, T0, T0 + 120 * NS, az, el, tracking)
    assert best is not None and best.mode == "normal" and best.max_error == 0


def test_plan_evaluates_between_samples(limits, tracking):
    az, el = limits
    target = GreatCircleTarget(T0, a0=10 * DEG, d=30 * DEG, rate=0.5 * DEG)
    p = planner.plan_mode("normal", target, T0, T0 + 60 * NS, az, el, tracking)
    t = T0 + 12_345_678_901
    a, e = p.mech(t)
    sky_az, sky_el = target.observed(t)
    assert abs(e[0] - sky_el) < 1e-12
    assert abs(np.interp(t, p.t_ns, p.a) - a[0]) < 1e-6  # same 2*pi branch as the plan


def test_sun_and_horizon_are_vetoed(site, limits, tracking):
    az, el = limits
    t = 1_780_000_000 * NS  # 2026-05-28
    s_az, s_el = astro.sun_observed(t, site)
    near_sun = FixedTarget(float(s_az), float(s_el) + 10 * DEG)
    p = planner.plan_mode("normal", near_sun, t, t + 30 * NS, az, el, tracking, site)
    assert any("Sun" in m for m in p.problems)
    low = FixedTarget(0.0, 5 * DEG)
    assert any("horizon" in m for m in planner.plan_mode("normal", low, t, t + 30 * NS, az, el, tracking).problems)


def test_pointing_model_shifts_the_commanded_angles(limits, tracking):
    az, el = limits
    target = FixedTarget(1.0, 0.7)
    model = PointingModel({"IA": 36.0, "IE": -18.0})
    p = planner.plan_mode("normal", target, T0, T0 + 10 * NS, az, el, tracking, model=model)
    arcsec = DEG / 3600
    assert abs((p.a[0] - 1.0) - 36 * arcsec) < 1e-9
    assert abs((p.e[0] - 0.7) + 18 * arcsec) < 1e-12
