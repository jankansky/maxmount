# SPDX-License-Identifier: GPL-3.0-or-later
"""ADS-B: aircraft.json parsing, the aircraft target's geometry and extrapolation, closest
approach, the simulator, and mount-svc tracking an aircraft from a live (simulated) feed."""
import json
import math
import threading
import time

import numpy as np
import pytest

from maxmount_svc import adsb, astro
from maxmount_svc.proto import mpb
from maxmount_svc.service import MountService

from .test_e2e_mount_rt import served  # noqa: F401 - fixture
from .test_service_e2e import StatusWatch, query

NS = 10**9


def offset_latlon(site, east_m, north_m):
    lat = site.latitude_deg + math.degrees(north_m / 6378137.0)
    lon = site.longitude_deg + math.degrees(east_m / (6378137.0 * math.cos(math.radians(site.latitude_deg))))
    return lat, lon


def one(site, east_m=0.0, north_m=0.0, alt_m=10000.0, gs_kt=0.0, track=0.0, rate_fpm=0, now=None, seen_pos=0.0, **extra):
    lat, lon = offset_latlon(site, east_m, north_m)
    ell = site.height_m + alt_m
    a = {"hex": "abc123", "flight": "TEST1   ", "lat": lat, "lon": lon, "alt_geom": ell / adsb.FT, "gs": gs_kt,
         "track": track, "geom_rate": rate_fpm, "seen_pos": seen_pos, **extra}
    return {"now": now if now is not None else time.time(), "aircraft": [a]}


def test_parse_fields_and_skips():
    doc = {"now": 1000.0, "aircraft": [
        {"hex": "a1b2c3", "flight": "UAL123  ", "lat": 34.0, "lon": -118.0, "alt_baro": 35000, "alt_geom": 35400,
         "gs": 450.0, "track": 90.0, "geom_rate": -1000, "seen_pos": 0.5, "t": "B738", "r": "N123UA", "squawk": "1234"},
        {"hex": "~0abcde", "lat": 34.1, "lon": -118.1, "alt_baro": 5000, "gs": 120, "track": 10},   # TIS-B, baro only
        {"hex": "ffffff", "lat": 34.2, "lon": -118.2, "alt_baro": "ground", "gs": 5, "track": 0},   # on the ground
        {"hex": "eeeeee", "alt_baro": 30000},                                                       # no position
    ]}
    d = adsb.parse(doc)
    assert set(d) == {"a1b2c3", "0abcde"}
    a = d["a1b2c3"]
    assert a.flight == "UAL123" and a.alt_source == "geom" and a.alt_m == pytest.approx(35400 * 0.3048)
    assert a.gs_m_s == pytest.approx(450 * 1852 / 3600) and a.vrate_m_s == pytest.approx(-1000 * 0.3048 / 60)
    assert a.t_pos_ns == 999_500_000_000 and a.type == "B738" and a.reg == "N123UA" and a.label == "UAL123"
    assert d["0abcde"].alt_source == "baro" and d["0abcde"].label == "0ABCDE"


def test_target_geometry_and_extrapolation(site):
    t = time.time_ns()
    up = adsb.AircraftTarget(site, adsb.parse(one(site, now=t / NS))["abc123"])
    az, el = up.observed(np.int64(t))
    assert math.degrees(el) == pytest.approx(90, abs=0.01)
    north45 = adsb.AircraftTarget(site, adsb.parse(one(site, north_m=10000, now=t / NS))["abc123"])
    az, el = north45.observed(np.int64(t))
    assert math.degrees(el) == pytest.approx(45, abs=0.2) and math.degrees(az) == pytest.approx(0, abs=0.1)
    # Flying east at 200 kt: 10 s later it is ~1029 m further east.
    east = adsb.AircraftTarget(site, adsb.parse(one(site, north_m=10000, gs_kt=200, track=90, now=t / NS))["abc123"])
    d = east.enu_m(np.array([t, t + 10 * NS], dtype=np.int64))
    assert d[1, 0] - d[0, 0] == pytest.approx(200 * 1852 / 3600 * 10, rel=1e-3) and abs(d[1, 1] - d[0, 1]) < 1
    lon, lat, h = east.lonlat(np.int64(t))
    assert lat[0] == pytest.approx(site.latitude_deg + math.degrees(10000 / 6378137), abs=1e-4)
    # A stale position is extrapolated from when it was received.
    late = adsb.AircraftTarget(site, adsb.parse(one(site, north_m=10000, gs_kt=200, track=90, now=t / NS, seen_pos=5))["abc123"])
    assert late.enu_m(np.int64(t))[0, 0] == pytest.approx(200 * 1852 / 3600 * 5, rel=1e-3)


def test_closest_approach(site):
    t = time.time_ns()
    # 20 km west, 5 km north, flying east at 300 kt, 3 km up: closest ~5.8 km, ~131 s later.
    a = adsb.parse(one(site, east_m=-20000, north_m=5000, alt_m=3000, gs_kt=300, track=90, now=t / NS))["abc123"]
    ca = adsb.closest_approach(adsb.AircraftTarget(site, a), t, horizon_s=600, step_s=1)
    assert ca["range_m"] == pytest.approx(math.hypot(5000, 3000), rel=0.01)
    assert (ca["t_ns"] - t) / NS == pytest.approx(20000 / (300 * 1852 / 3600), abs=1.5)
    assert math.degrees(ca["el_rad"]) == pytest.approx(math.degrees(math.atan2(3000, 5000)), abs=0.5)
    assert ca["max_rate_rad_s"] == pytest.approx(300 * 1852 / 3600 / math.hypot(5000, 3000), rel=0.05)


def test_simulator_round_trips(site, tmp_path):
    doc = adsb.simulate(site, 1_000_000.0, n=6)
    d = adsb.parse(json.loads(json.dumps(doc)))
    assert len(d) == 6 and all(a.alt_source == "geom" and a.type == "A320" for a in d.values())
    # Deterministic for a seed, and each aircraft moves in a straight line.
    later = adsb.parse(adsb.simulate(site, 1_000_010.0, n=6))
    a, b = d["a00000"], later["a00000"]
    tgt = adsb.AircraftTarget(site, a)
    lon, lat, _h = tgt.lonlat(np.int64(b.t_pos_ns))
    assert lat[0] == pytest.approx(b.lat_deg, abs=2e-4) and lon[0] == pytest.approx(b.lon_deg, abs=2e-4)
    # The file source reads what the simulator writes, with a trail.
    path = tmp_path / "aircraft.json"
    src = adsb.AdsbSource(str(path), min_interval_s=0)
    assert src.aircraft() == {} and not src.ok and "no ADS-B data" in src.note
    t = time.time()  # the trail keeps the last 5 minutes
    path.write_text(json.dumps(adsb.simulate(site, t, n=6)))
    assert len(src.aircraft()) == 6 and src.ok
    path.write_text(json.dumps(adsb.simulate(site, t + 1, n=6)))
    assert len(src.get("A00000").trail) == 2


def test_mount_svc_tracks_an_aircraft_and_holds_when_the_feed_stops(served, tmp_path):  # noqa: F811
    client, settings = served
    path = tmp_path / "aircraft.json"
    settings.adsb.source = str(path)
    settings.adsb.max_age_s = 3.0
    # Put the aircraft opposite the Sun (the plan keeps 30 deg from it), ~40 deg up, 12 km
    # away, flying slowly across the line of sight so it stays up for minutes.
    sun_az, _ = astro.sun_observed(np.int64(time.time_ns()), settings.site)
    az = float(sun_az) + math.pi
    rng_h = 12000.0
    t0 = time.time()
    stop = threading.Event()

    def feed():
        while not stop.is_set():
            t = time.time()
            along = 60 * (t - t0)  # 60 m/s, perpendicular to the line of sight
            e = rng_h * math.sin(az) + along * math.cos(az)
            n = rng_h * math.cos(az) - along * math.sin(az)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(one(settings.site, east_m=e, north_m=n, alt_m=rng_h * 0.84, now=t,
                                          gs_kt=60 / (1852 / 3600), track=math.degrees(az) + 90, seen_pos=0.1)))
            tmp.replace(path)
            time.sleep(0.3)

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    time.sleep(0.5)
    svc = MountService(settings, client)
    svc.start()
    from maxmount_svc.mount import MountClient

    with MountClient(settings.zenoh) as gui:
        watch = StatusWatch(gui)
        try:
            rep = query(gui, "svc/target", mpb.TargetCommand(track_aircraft=mpb.TrackAircraft(icao="nope")), mpb.TargetReply)
            assert not rep.accepted and "not in the ADS-B data" in rep.message
            rep = query(gui, "svc/target", mpb.TargetCommand(track_aircraft=mpb.TrackAircraft(icao="ABC123")), mpb.TargetReply)
            assert rep.accepted, rep.message
            st = watch.wait(lambda s: s.state == mpb.SERVICE_STATE_TRACKING and s.target_kind == "aircraft", timeout=60)
            assert st.target_name == "TEST1" and st.aircraft_icao == "abc123"
            assert 12 < st.range_km < 18 and st.altitude_km == pytest.approx((settings.site.height_m + rng_h * 0.84) / 1000, abs=0.05)  # above the ellipsoid
            assert 35 < st.target_el_deg < 45
            # The mount follows the live feed: its azimuth keeps up with the aircraft.
            time.sleep(3)
            tel = client.telemetry(fresh=True)
            az_now = {a.axis: a.act_position_rad for a in tel.axes}["az"]
            st = watch.last
            assert abs((math.degrees(az_now) - st.target_az_deg + 180) % 360 - 180) < 0.5
            # The feed stops: after adsb.max_age_s the mount holds (stays enabled).
            stop.set()
            st = watch.wait(lambda s: "ADS-B position" in s.message, timeout=20)
            assert st.state == mpb.SERVICE_STATE_IDLE and "holding" in st.message
            assert client.telemetry(fresh=True).enabled
        finally:
            stop.set()
            watch.sub.undeclare()
            svc.close()
