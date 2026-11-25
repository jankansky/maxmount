# SPDX-License-Identifier: GPL-3.0-or-later
import numpy as np

from maxmount_svc import kinematics as k

from .conftest import DEG


def test_both_branches_point_at_the_same_sky_direction():
    az = np.radians([0, 30, 179, 300])
    el = np.radians([5, 45, 89, 60])
    for flipped in (False, True):
        a, e = k.to_mech(az, el, flipped)
        az2, el2 = k.from_mech(a, e)
        assert np.allclose(np.mod(az2 - az + np.pi, 2 * np.pi) - np.pi, 0, atol=1e-12)
        assert np.allclose(el2, el)
    a, e = k.to_mech(az, el, True)
    assert np.all(e >= np.pi / 2)


def test_unwrap_fits_the_cable_wrap_and_prefers_short_slews():
    path = np.radians(np.linspace(350, 370, 50)) % (2 * np.pi)  # crosses north
    a = k.unwrap_into(path, -270 * DEG, 270 * DEG)
    assert np.all(np.diff(a) > 0) and a.min() >= -270 * DEG and a.max() <= 270 * DEG
    near = k.unwrap_into(path, -270 * DEG, 270 * DEG, near=-10 * DEG)
    assert abs(near[0] - (-10 * DEG)) < 1e-9
    assert k.unwrap_into(np.radians(np.linspace(0, 600, 100)) % (2 * np.pi), -270 * DEG, 270 * DEG) is None
