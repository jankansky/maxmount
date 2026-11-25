# SPDX-License-Identifier: GPL-3.0-or-later
import calendar
import math

import erfa
import numpy as np

from maxmount_svc import astro
from maxmount_svc.targets import SiderealTarget

from .conftest import DEG, NS

ARCSEC = DEG / 3600


def test_teme_to_pef_matches_vallado_example():
    # Vallado et al. 2006, "Revisiting Spacetrack Report #3", TEME -> PEF example.
    t = calendar.timegm((2004, 4, 6, 7, 51, 28)) * NS + 386_009_000
    r = astro.teme_to_pef(np.array([5094.18016210, 6127.64465950, 6380.34453270]), t, -0.4399619)
    assert np.allclose(r, [-1033.47503130, 7901.30558560, 6380.34453270], atol=2e-5)  # 2 cm


def test_celestial_pole_altitude_is_latitude_plus_refraction(site):
    t = calendar.timegm((2026, 1, 1, 0, 0, 0)) * NS
    # The pole of date (CIP), not the ICRS pole: precession has moved it ~8' since J2000.
    rbpn = erfa.pnm06a(*astro.tt_jd(t))
    ra, dec = erfa.c2s(rbpn[2])
    pole = SiderealTarget(site, float(ra), float(dec))
    _, el = pole.observed(t)
    refraction = el - site.latitude_deg * DEG
    # ~70" at 34 deg altitude and 830 hPa; annual aberration shifts the pole by up to 21".
    assert 40 * ARCSEC < refraction < 100 * ARCSEC
    site.pressure_hpa = 0  # no air: the pole sits at the geodetic latitude (to aberration)
    _, el0 = pole.observed(t)
    assert abs(el0 - site.latitude_deg * DEG) < 21 * ARCSEC


def test_star_returns_after_one_sidereal_day(site):
    t = calendar.timegm((2026, 3, 1, 6, 0, 0)) * NS
    star = SiderealTarget(site, 1.0, 0.3)
    az1, el1 = star.observed(t)
    az2, el2 = star.observed(t + int(86164.0905 * NS))
    assert astro.separation(az1, el1, az2, el2) < 2 * ARCSEC


def test_sun_near_zenith_at_equinox_noon():
    from maxmount_svc.config import Site

    equator = Site(latitude_deg=0.0, longitude_deg=0.0, pressure_hpa=0)
    t = calendar.timegm((2024, 3, 20, 12, 7, 0)) * NS  # transit at lon 0 (equation of time ~ -7 min)
    _, el = astro.sun_observed(t, equator)
    assert el > 89 * DEG


def test_refraction_lifts_targets(site):
    z = np.radians([10.0, 45.0, 70.0])
    dz = astro.refraction_dz(z, site)
    assert np.all(dz > 0) and np.all(np.diff(dz) > 0)
    refa, _ = erfa.refco(site.pressure_hpa, site.temperature_c, site.humidity, site.wavelength_um)
    assert abs(dz[1] - refa) < 0.5 * ARCSEC  # tan 45 = 1
