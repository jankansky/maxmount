# SPDX-License-Identifier: GPL-3.0-or-later
import math

import numpy as np

from maxmount_svc import astro, planner
from maxmount_svc.targets import SatelliteTarget

from .conftest import DEG, ISS, NS


def test_passes_are_found_and_plausible(site):
    sat = SatelliteTarget(site, *ISS, name="ISS")
    t0 = sat.epoch_ns
    passes = planner.find_passes(sat, t0, t0 + 24 * 3600 * NS, 10 * DEG)
    assert 2 <= len(passes) <= 8
    for p in passes:
        assert 0 < p.duration_s < 12 * 60
        assert 10 * DEG < p.max_el <= 90 * DEG
        _, el = sat.observed(np.array([p.rise_ns, p.culmination_ns, p.set_ns]))
        assert abs(el[0] - 10 * DEG) < 0.05 * DEG and abs(el[2] - 10 * DEG) < 0.05 * DEG
        assert el[1] > el[0]


def test_scalar_and_array_agree(site):
    sat = SatelliteTarget(site, *ISS)
    t = sat.epoch_ns + 1234 * NS
    az, el = sat.observed(t)
    az_a, el_a = sat.observed(np.array([t, t + NS]))
    assert az == az_a[0] and el == el_a[0]


def test_time_offset_is_along_track(site):
    t = SatelliteTarget(site, *ISS).epoch_ns + 4000 * NS
    late = SatelliteTarget(site, *ISS, time_offset_s=1.5)
    plain = SatelliteTarget(site, *ISS)
    assert np.allclose(late.observed(t), plain.observed(t + int(1.5 * NS)), atol=1e-12)


def test_cross_track_offset_moves_perpendicular(site):
    sat = SatelliteTarget(site, *ISS)
    t0 = sat.epoch_ns
    p = planner.find_passes(sat, t0, t0 + 24 * 3600 * NS, 10 * DEG)[0]
    t = p.culmination_ns
    shifted = SatelliteTarget(site, *ISS, cross_track_rad=0.5 * DEG)
    a0, e0 = sat.observed(t)
    a1, e1 = shifted.observed(t)
    assert math.isclose(astro.separation(a0, e0, a1, e1), 0.5 * DEG, rel_tol=0.02)  # refraction differs slightly
    # Perpendicular: the shift is orthogonal to the motion over the next second.
    u0 = astro.azel_to_enu(a0, e0)
    u1 = astro.azel_to_enu(*shifted.observed(t))
    motion = astro.azel_to_enu(*sat.observed(t + NS)) - u0
    assert abs(np.dot(u1 - u0, motion)) < 0.05 * np.linalg.norm(u1 - u0) * np.linalg.norm(motion)
