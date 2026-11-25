# SPDX-License-Identifier: GPL-3.0-or-later
import math

import numpy as np
import pytest

from maxmount_svc import astro
from maxmount_svc.config import AxisLimits, Site, Tracking

DEG = math.pi / 180
NS = 1_000_000_000

# Classic ISS element set (2008-09-20), valid checksums; tests use times near its epoch.
ISS = (
    "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927",
    "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537",
)


@pytest.fixture
def site():
    return Site(latitude_deg=34.2247, longitude_deg=-118.0570, height_m=1742, pressure_hpa=830, temperature_c=10)


@pytest.fixture
def limits():
    az = AxisLimits(-270 * DEG, 270 * DEG, 30 * DEG, 20 * DEG)
    el = AxisLimits(-45 * DEG, 180 * DEG, 10 * DEG, 10 * DEG)
    return az, el


@pytest.fixture
def tracking():
    return Tracking(horizon_min_el_deg=10, sun_min_sep_deg=30, over_the_top_tolerance_deg=1.5)


class GreatCircleTarget:
    """Synthetic pass: moves at `rate` along a vertical great circle (azimuth a0), offset `d`
    from it; closest approach to the zenith is `d`. Starts `start` rad above the a0 horizon."""

    def __init__(self, t0_ns, a0, d, rate, start=10 * DEG, name="synthetic"):
        self.t0, self.a0, self.d, self.rate, self.start, self.name = t0_ns, a0, d, rate, start, name

    def observed(self, t_ns):
        s = self.start + self.rate * (np.asarray(t_ns, dtype=np.int64) - self.t0) / NS
        h = np.array([math.sin(self.a0), math.cos(self.a0), 0.0])
        up = np.array([0.0, 0.0, 1.0])
        n = np.array([math.cos(self.a0), -math.sin(self.a0), 0.0])
        s = np.asarray(s)[..., None]
        v = math.cos(self.d) * (np.cos(s) * h + np.sin(s) * up) + math.sin(self.d) * n
        return astro.enu_to_azel(v)
