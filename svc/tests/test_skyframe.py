# SPDX-License-Identifier: GPL-3.0-or-later
"""Sky chart astronomy (skyframe.py) and its gateway requests (skyops.py)."""
import datetime as dt
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from maxmount_svc import astro, skyframe
from maxmount_svc.config import Site, Tracking
from maxmount_svc.proto import mpb
from maxmount_svc.skyops import SkyOps

from .test_e2e_mount_rt import served  # noqa: F401 - fixture
from .test_service_e2e import query, service  # noqa: F401 - fixture

T = int(dt.datetime(2026, 3, 1, 4, 0, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000
# JPL Horizons, 2026-03-01 04:00 UT, geocentric astrometric RA/Dec (ICRF), deg.
HORIZONS = {
    "sun": (341.731291059, -7.738496293), "moon": (134.219275665, 19.807224042),
    "mercury": (350.919612915, -0.303561291), "venus": (354.165242741, -4.000208007),
    "mars": (331.055474370, -13.016053925), "jupiter": (106.191396915, 22.948314783),
    "saturn": (2.115212562, -1.405620694), "uranus": (55.134411823, 19.394484467),
    "neptune": (1.150937729, -0.925617909),
}
GREENWICH = Site(latitude_deg=51.4779, longitude_deg=0.0, height_m=46.0, pressure_hpa=0)


def sep_arcsec(ra1, dec1, ra2, dec2):
    r = np.radians
    c = math.sin(r(dec1)) * math.sin(r(dec2)) + math.cos(r(dec1)) * math.cos(r(dec2)) * math.cos(r(ra1 - ra2))
    return math.degrees(math.acos(min(1.0, c))) * 3600


@pytest.mark.parametrize("body", list(HORIZONS))
def test_geocentric_positions_match_horizons(body):
    g = skyframe.geocentric(body, np.int64(T))
    ra, dec = math.degrees(math.atan2(g[1], g[0])) % 360, math.degrees(math.asin(g[2] / np.linalg.norm(g)))
    limit = 20 if body == "moon" else 60  # moon98 ~10"; plan94 arcseconds to tens of arcseconds
    assert sep_arcsec(ra, dec, *HORIZONS[body]) < limit


def test_topocentric_moon_matches_horizons():
    """Horizons, Greenwich, airless apparent az/el: 278.375225, 17.810262. Parallax is ~0.9 deg."""
    az, el, _d = skyframe.body_topocentric("moon", np.atleast_1d(np.int64(T)), GREENWICH)
    d_az = (math.degrees(az[0]) - 278.375225) * math.cos(math.radians(17.81)) * 3600
    d_el = (math.degrees(el[0]) - 17.810262) * 3600
    assert math.hypot(d_az, d_el) < 30


def test_frame_matrix_matches_erfa(site):
    R = skyframe.frame_matrix(T, site)
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-12) and np.linalg.det(R) > 0
    rng = np.random.default_rng(1)
    ra, dec = rng.uniform(0, 2 * np.pi, 300), np.arcsin(rng.uniform(-1, 1, 300))
    az, el = skyframe._geometric_azel(np.int64(T), site, ra, dec)
    v = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], -1)
    err = np.degrees(np.arccos(np.clip(np.sum((v @ R.T) * astro.azel_to_enu(az, el), -1), -1, 1))) * 3600
    assert err.max() < 30  # annual aberration is not a rotation: ~20" at most


def test_body_target_and_solar_system(site):
    moon = skyframe.BodyTarget(site, "moon")
    t = np.array([T, T + 3600 * 1_000_000_000], dtype=np.int64)
    az, el = moon.observed(t)
    a0, e0 = moon.observed(np.int64(T))
    assert az.shape == (2,) and abs(az[0] - a0) < 1e-12 and abs(el[0] - e0) < 1e-12
    with pytest.raises(ValueError):
        skyframe.BodyTarget(site, "pluto")
    bodies = {b["body"]: b for b in skyframe.solar_system(T, site)}
    assert set(bodies) == set(skyframe.BODIES)
    assert 1700 < bodies["moon"]["diameter_arcsec"] < 2100 and 1900 < bodies["sun"]["diameter_arcsec"] < 1960
    assert bodies["venus"]["mag"] < -3.5 and 7.5 < bodies["neptune"]["mag"] < 8.1
    assert 0 <= bodies["moon"]["phase"] <= 1 and 0 <= bodies["moon"]["bright_limb_pa_deg"] < 360
    # The chart's RA/Dec for a body, put through the same matrix, gives back its az/el.
    R = skyframe.frame_matrix(T, site)
    b = bodies["jupiter"]
    ra, dec = math.radians(b["ra_deg"]), math.radians(b["dec_deg"])
    enu = R @ np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])
    _az, el_geo = astro.enu_to_azel(enu)
    assert abs(math.degrees(float(skyframe.refract(el_geo, site))) - b["el_deg"]) < 1e-6


def test_events_and_darkness():
    t = np.arange(0, 25, dtype=np.int64) * 3600 * 1_000_000_000
    alt = np.radians(40 * np.sin(2 * np.pi * (np.arange(25) - 3) / 24))  # rises at 3 h, peaks at 9 h
    ev = skyframe.events(t, alt, math.radians(10))
    assert ev["state"] == "rises and sets" and not ev["up_now"]
    assert abs(ev["rise_ns"] / 3.6e12 - 4.0) < 0.1 and abs(ev["set_ns"] / 3.6e12 - 14.0) < 0.1
    assert abs(ev["transit_ns"] / 3.6e12 - 9) < 0.05 and abs(ev["transit_alt_deg"] - 40) < 0.2
    assert skyframe.events(t, alt + 1.0, 0.1)["state"] == "circumpolar"
    sun = -alt  # dark (Sun below -12) while the "object" is above 12 deg
    start, end = skyframe.darkness(t, sun, -12)
    assert start == 5 * 3_600_000_000_000 and end == 14 * 3_600_000_000_000  # hourly samples


def test_search_finds_planets_stars_and_dsos():
    assert skyframe.search("jup")[0]["body"] == "jupiter"
    names = [h["name"] for h in skyframe.search("M 31")]
    assert names[0] == "M 31"
    assert skyframe.search("vega")[0]["name"] == "Vega"


@pytest.fixture
def ops(site):
    return SkyOps(SimpleNamespace(site=site, tracking=Tracking(horizon_min_el_deg=10, sun_min_sep_deg=30)))


def test_sky_ops(ops):
    ok, _msg, f = ops.handle("sky_frame", {})
    assert ok and len(f["R"]) == 9 and f["refa"] > 0 and len(f["bodies"]) == 9 and f["mask_deg"] == 10
    ok, _msg, s = ops.handle("sky_search", {"q": "saturn"})
    assert ok and s["results"][0]["kind"] == "body" and "ra_deg" in s["results"][0]
    # Polaris from 34 deg N: circumpolar, at ~34 deg.
    ok, _msg, o = ops.handle("sky_object", {"ra_deg": 37.95, "dec_deg": 89.26})
    assert ok and o["state"] == "circumpolar" and abs(o["el_deg"] - 34.2) < 1.5
    ok, _msg, o = ops.handle("sky_object", {"body": "moon"})
    assert ok and o["state"] in ("rises and sets", "circumpolar", "never rises")
    ok, msg, n = ops.handle("sky_tonight", {"min_alt_deg": 30, "types": ["galaxy"], "sort": "size"})
    assert ok, msg
    if n["dark"]:  # always true at 34 deg N
        objs = n["objects"]
        assert objs and all(o["type"] == "galaxy" and o["max_alt_deg"] >= 30 for o in objs)
        assert [o["maj"] for o in objs] == sorted((o["maj"] for o in objs), reverse=True)
        assert all(n["dark"]["start_ns"] <= o["best_ns"] <= n["dark"]["end_ns"] for o in objs)
    ok, _msg, _ = ops.handle("sky_nope", {})
    assert not ok


def test_sky_ops_without_a_site():
    ok, msg, _ = SkyOps(SimpleNamespace(site=None, tracking=None)).handle("sky_frame", {})
    assert not ok and "site" in msg


def test_body_target_planned_by_mount_svc(site, limits, tracking):
    """A body is planned like a star: the planner follows it while it is up."""
    from maxmount_svc import planner
    from maxmount_svc.pointing import PointingModel

    az, el = limits
    now = time.time_ns()
    up = [b for b in skyframe.solar_system(now, site) if b["el_deg"] > 25 and b["body"] != "sun"]
    if not up:
        pytest.skip("no body well up right now")
    target = skyframe.BodyTarget(site, up[0]["body"])
    p = planner.plan_mode("normal", target, now, now + 600 * 1_000_000_000, az, el, tracking, site, PointingModel(), None, 1.0)
    assert p is not None


def test_mount_svc_tracks_a_body(service):  # noqa: F811
    """track_body end to end: mount-svc plans and tracks a solar-system body that is up."""
    _svc, _mount, gui, watch, settings = service
    now = time.time_ns()
    bodies = skyframe.solar_system(now, settings.site)
    sun = next(b for b in bodies if b["body"] == "sun")
    up = [b for b in bodies if b["body"] != "sun" and b["el_deg"] > 20
          and sep_arcsec(b["ra_deg"], b["dec_deg"], sun["ra_deg"], sun["dec_deg"]) / 3600 > 40]
    if not up:
        pytest.skip("no body well up and away from the Sun right now")
    body = up[0]["body"]
    cmd = mpb.TargetCommand(track_body=mpb.SolarSystemBody(body=body, duration_s=60))
    rep = query(gui, "svc/target", cmd, mpb.TargetReply)
    assert rep.accepted, rep.message
    st = watch.wait(lambda s: s.target_name == body.capitalize())
    assert st.target_kind == "body"
    assert abs(st.target_el_deg - up[0]["el_deg"]) < 1.0
    rep = query(gui, "svc/target", mpb.TargetCommand(track_body=mpb.SolarSystemBody(body="pluto")), mpb.TargetReply)
    assert not rep.accepted and "unknown body" in rep.message
    assert query(gui, "svc/target", mpb.TargetCommand(stop=mpb.Stop()), mpb.TargetReply).accepted


@pytest.mark.parametrize("which", ["moon", "saturn", "star"])
def test_sampled_target_matches_exact(site, which):
    """Planning interpolates fixed-sky targets from 60 s samples: it must agree with the exact
    ERFA path to far below an arcsecond above 5 deg (below ~3 deg the refraction formula's
    zenith-distance clamp puts a kink in the path; the horizon mask is well above that), and
    fall back to the exact path outside its span."""
    from maxmount_svc.targets import Sampled, SiderealTarget
    exact = (SiderealTarget(site, math.radians(279.23), math.radians(38.78), name="Vega") if which == "star"
             else skyframe.BodyTarget(site, which))
    t0 = time.time_ns()
    s = Sampled(exact, t0, t0 + 12 * 3600 * 10**9)
    t = t0 + np.arange(0, 12 * 3600, 7.3).astype(np.int64) * 10**9
    a1, e1 = exact.observed(t)
    a2, e2 = s.observed(t)
    sep = astro.separation(a1, e1, a2, e2)
    up = e1 > math.radians(5)
    assert np.degrees(sep[up].max()) * 3600 < 0.01
    assert s.name == exact.name
    out = t0 + 13 * 3600 * 10**9
    assert s.observed(np.int64(out)) == pytest.approx(exact.observed(np.int64(out)))
