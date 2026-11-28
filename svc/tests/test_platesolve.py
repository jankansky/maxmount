# SPDX-License-Identifier: GPL-3.0-or-later
"""The built-in plate solver on frames rendered by the widefield simulator."""
import math

import numpy as np
import pytest

from maxmount_svc import kinematics, platesolve
from maxmount_svc.config import Site, WidefieldSettings
from maxmount_svc.widefield import ObservedFrame, icrs_of, render_sky

DEG = math.pi / 180
ARCSEC = DEG / 3600
T_NS = 1_768_453_200 * 1_000_000_000  # 2026-01-15 05:00 UTC: night at the example site
SITE = Site(latitude_deg=34.2247, longitude_deg=-118.057, height_m=1742, pressure_hpa=830, temperature_c=10)


def _sep(ra1, dec1, ra2, dec2):
    return math.acos(min(1.0, math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)))


def _offset(ra, dec, dist, pa):
    """A point dist (rad) from (ra, dec) at position angle pa."""
    d2 = math.asin(math.sin(dec) * math.cos(dist) + math.cos(dec) * math.sin(dist) * math.cos(pa))
    r2 = ra + math.atan2(math.sin(pa) * math.sin(dist) * math.cos(dec), math.cos(dist) - math.sin(dec) * math.sin(d2))
    return r2 % (2 * math.pi), d2


@pytest.mark.parametrize("a_deg,e_deg,rot,flip,seed", [(120, 50, 37, False, 1), (300, 25, -70, False, 2),
                                                      (45, 75, 160, True, 3), (210, 120, 5, False, 4),
                                                      (10, 40, 90, False, 5),   # Dec +80: near the pole
                                                      (200, 15, 0, False, 6)])  # low: strong refraction
def test_solves_with_a_hint_3_deg_off_and_unknown_roll(a_deg, e_deg, rot, flip, seed):
    cfg = WidefieldSettings()
    rng = np.random.default_rng(seed)
    a, e = a_deg * DEG, e_deg * DEG
    img = render_sky(cfg, SITE, T_NS, a, e, 1.0, 10, rng, rotation_deg=rot, flip=flip).astype(np.float32)
    az, el = (float(v) for v in kinematics.from_mech(a, e))
    ra, dec = icrs_of(SITE, T_NS, az, el)
    hint = _offset(ra, dec, 3 * DEG, rng.uniform(0, 2 * math.pi))
    scale = cfg.arcsec_per_px * ARCSEC * 1.06  # the lens is a little off its nominal focal length
    # In the observed frame (what the widefield service uses) refraction is modelled: sub-arcsecond.
    sol = platesolve.solve(img, *hint, scale, search_radius=4 * DEG, year=2026.04, frame=ObservedFrame(SITE, T_NS))
    assert sol.ok, sol.message
    assert _sep(sol.ra, sol.dec, ra, dec) / ARCSEC < 3, sol.message
    assert sol.matched >= 40
    assert sol.arcsec_per_px == pytest.approx(cfg.arcsec_per_px, rel=0.01)
    assert sol.solve_s < 10
    # In ICRS, differential refraction bends the field: still solved, the centre within ~25".
    sol = platesolve.solve(img, *hint, scale, search_radius=4 * DEG, year=2026.04)
    assert sol.ok, sol.message
    assert _sep(sol.ra, sol.dec, ra, dec) / ARCSEC < (30 if e_deg < 20 else 8)


def test_mirrored_camera_reports_parity():
    cfg = WidefieldSettings()
    out = []
    for flip in (False, True):
        img = render_sky(cfg, SITE, T_NS, 1.0, 0.9, 1.0, 10, np.random.default_rng(8), rotation_deg=30, flip=flip)
        az, el = (float(v) for v in kinematics.from_mech(1.0, 0.9))
        ra, dec = icrs_of(SITE, T_NS, az, el)
        sol = platesolve.solve(img.astype(np.float32), ra, dec, cfg.arcsec_per_px * ARCSEC)
        assert sol.ok
        out.append(sol.mirrored)
    assert out[0] != out[1]


def test_cloudy_frame_is_a_clean_failure():
    cfg = WidefieldSettings()
    img = render_sky(cfg, SITE, T_NS, 1.0, 0.8, 1.0, 10, np.random.default_rng(5), cloudy=True)
    sol = platesolve.solve(img.astype(np.float32), 1.0, 0.5, cfg.arcsec_per_px * ARCSEC)
    assert not sol.ok and sol.message.startswith("no solution")


def test_wrong_place_is_not_a_false_solution():
    """A hint 40 deg away from where the frame points finds nothing rather than a wrong answer."""
    cfg = WidefieldSettings()
    a, e = 2.0, 0.9
    img = render_sky(cfg, SITE, T_NS, a, e, 1.0, 10, np.random.default_rng(6))
    az, el = (float(v) for v in kinematics.from_mech(a, e))
    ra, dec = icrs_of(SITE, T_NS, az, el)
    hint = _offset(ra, dec, 40 * DEG, 1.0)
    sol = platesolve.solve(img.astype(np.float32), *hint, cfg.arcsec_per_px * ARCSEC, search_radius=4 * DEG)
    assert not sol.ok


def test_astap_ini_parsing():
    ini = "\n".join(["PLTSOLVD=T", "CRPIX1=960.5", "CRPIX2=600.5", "CRVAL1=83.8", "CRVAL2=-5.4",
                     "CD1_1=-0.0096", "CD1_2=0.0", "CD2_1=0.0", "CD2_2=0.0096", "STARS=120"])
    sol = platesolve.parse_astap_ini(ini, 1920, 1200)
    assert sol.ok and math.degrees(sol.ra) == pytest.approx(83.8) and math.degrees(sol.dec) == pytest.approx(-5.4)
    assert sol.arcsec_per_px == pytest.approx(34.56)
    assert not platesolve.parse_astap_ini("PLTSOLVD=F\nWARNING=no stars", 10, 10).ok


@pytest.mark.parametrize("k1", [0.012, -0.008])
def test_fits_lens_distortion(k1):
    """A lens with ~1 % radial distortion at the corners: fitted, and the centre and scale stay right."""
    from dataclasses import replace

    cfg = replace(WidefieldSettings(), sim_distortion_k1=k1)
    a, e = 150 * DEG, 55 * DEG
    img = render_sky(cfg, SITE, T_NS, a, e, 1.0, 10, np.random.default_rng(9), rotation_deg=20).astype(np.float32)
    az, el = (float(v) for v in kinematics.from_mech(a, e))
    ra, dec = icrs_of(SITE, T_NS, az, el)
    scale = cfg.arcsec_per_px * ARCSEC
    sol = platesolve.solve(img, ra + 1 * DEG, dec, scale, search_radius=4 * DEG, year=2026.04, frame=ObservedFrame(SITE, T_NS))
    assert sol.ok, sol.message
    assert sol.k1 == pytest.approx(k1, abs=0.0015)
    assert "distortion" in sol.message
    assert _sep(sol.ra, sol.dec, ra, dec) / ARCSEC < 5
    assert sol.rms_arcsec < 18  # ~0.4 px: the simulated centroid noise, as with no distortion (13.8")
    off = platesolve.solve(img, ra + 1 * DEG, dec, scale, search_radius=4 * DEG, year=2026.04,
                           frame=ObservedFrame(SITE, T_NS), fit_distortion=False)
    assert not off.ok or off.rms_arcsec > 1.5 * sol.rms_arcsec  # without the term the fit is clearly worse
