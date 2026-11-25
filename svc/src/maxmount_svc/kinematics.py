# SPDX-License-Identifier: GPL-3.0-or-later
"""Alt-az mount whose elevation axis travels past the zenith (0..180 deg).

A sky direction (az, el) is reached by two mechanical poses:
    normal   A = az,        E = el
    flipped  A = az + 180,  E = 180 - el
They coincide only at the zenith. A continuous sky path that avoids the zenith therefore has
exactly two continuous mechanical lifts, one per branch, and their azimuth rates are identical.
Going "over the top" does not remove the zenith keyhole; what it adds is the option of holding
azimuth fixed along a pass and sweeping elevation through the zenith, with a pointing error
bounded by the pass's closest approach to the zenith (see planner.over_the_top).
"""
from __future__ import annotations

import numpy as np

from . import astro

TWO_PI = 2 * np.pi


def to_mech(az, el, flipped: bool):
    az = np.asarray(az, dtype=float)
    el = np.asarray(el, dtype=float)
    if flipped:
        return np.mod(az + np.pi, TWO_PI), np.pi - el
    return np.mod(az, TWO_PI), el.copy()


def from_mech(a, e):
    """Mechanical pose -> sky (az, el) direction."""
    a = np.asarray(a, dtype=float)
    e = np.asarray(e, dtype=float)
    over = e > np.pi / 2
    az = np.where(over, a + np.pi, a)
    el = np.where(over, np.pi - e, e)
    return np.mod(az, TWO_PI), el


def unwrap_into(a, a_min: float, a_max: float, near: float | None = None):
    """Unwraps azimuths into a continuous path and shifts it by 2*pi*k so it lies within
    [a_min, a_max]. With `near` (the current axis position) the shift needing the shortest
    slew to the start wins; otherwise the path is kept central in the cable-wrap range.
    Returns None if no shift fits (the path needs more cable wrap than the axis has)."""
    a = np.unwrap(np.asarray(a, dtype=float))
    k_min = int(np.ceil((a_min - a.min()) / TWO_PI))
    k_max = int(np.floor((a_max - a.max()) / TWO_PI))
    if k_min > k_max:
        return None
    if near is not None:
        cost = lambda k: abs(a[0] + k * TWO_PI - near)  # noqa: E731
    else:
        mid = 0.5 * (a_min + a_max)
        cost = lambda k: abs(0.5 * (a.min() + a.max()) + k * TWO_PI - mid)  # noqa: E731
    return a + min(range(k_min, k_max + 1), key=cost) * TWO_PI


def direction(a, e):
    """Unit pointing vector (east, north, up) of a mechanical pose."""
    return astro.azel_to_enu(*from_mech(a, e))
