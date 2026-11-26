# SPDX-License-Identifier: GPL-3.0-or-later
"""Aircraft from a local ADS-B receiver: readsb / dump1090 aircraft.json.

readsb writes aircraft.json about once a second (default /run/readsb/aircraft.json; tar1090
serves it at /tar1090/data/aircraft.json). Fields used, per aircraft (readsb's README-json):

    hex        ICAO 24-bit address        flight     callsign (padded)
    lat, lon   degrees                    seen_pos   seconds since the position was received
    alt_geom   ft above the WGS84 ellipsoid (GNSS); alt_baro ft (pressure altitude) as a fallback
    gs         ground speed, knots        track      true track over ground, degrees
    geom_rate / baro_rate  ft/min         t, r       type and registration (when readsb has a database)
    category, squawk

An aircraft is extrapolated in a straight line (Earth-fixed) from its last position, along its
ground track at its ground speed and climb rate: good for seconds to a minute or two; turns are
not predicted. Pressure altitude can differ from geometric height by a few hundred feet, which
is ~1 deg at 10 km range: the camera loop corrects what ADS-B cannot.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import erfa
import numpy as np

from . import astro

NS = 1_000_000_000
FT = 0.3048
KT = 1852.0 / 3600.0
TRAIL_S = 300


@dataclass
class Aircraft:
    icao: str
    flight: str
    lat_deg: float
    lon_deg: float
    alt_m: float                 # above the WGS84 ellipsoid (or pressure altitude, see alt_source)
    alt_source: str              # "geom" or "baro"
    gs_m_s: float
    track_deg: float
    vrate_m_s: float
    t_pos_ns: int                # when the position was received
    type: str = ""
    reg: str = ""
    category: str = ""
    squawk: str = ""
    trail: list = field(default_factory=list)  # [(t_ns, lat, lon, alt_m)], oldest first

    @property
    def label(self) -> str:
        return self.flight or self.reg or self.icao.upper()

    def ecef_m(self) -> tuple[np.ndarray, np.ndarray]:
        """Position (m) at t_pos_ns and velocity (m/s), Earth-fixed."""
        lat, lon = math.radians(self.lat_deg), math.radians(self.lon_deg)
        p = np.asarray(erfa.gd2gc(1, lon, lat, self.alt_m))
        tr = math.radians(self.track_deg)
        e, n, u = self.gs_m_s * math.sin(tr), self.gs_m_s * math.cos(tr), self.vrate_m_s
        sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
        east = np.array([-so, co, 0.0])
        north = np.array([-sl * co, -sl * so, cl])
        up = np.array([cl * co, cl * so, sl])
        return p, e * east + n * north + u * up


def parse(doc: dict) -> dict[str, Aircraft]:
    """aircraft.json -> {icao: Aircraft} for aircraft with a usable position and velocity."""
    now_s = float(doc.get("now") or time.time())
    out = {}
    for a in doc.get("aircraft") or []:
        try:
            if "lat" not in a or "lon" not in a or "gs" not in a or "track" not in a:
                continue
            if isinstance(a.get("alt_geom"), (int, float)):
                alt, src = a["alt_geom"] * FT, "geom"
            elif isinstance(a.get("alt_baro"), (int, float)):
                alt, src = a["alt_baro"] * FT, "baro"
            else:
                continue  # "ground" or missing
            rate = a.get("geom_rate", a.get("baro_rate", 0)) or 0
            icao = str(a["hex"]).lower().lstrip("~")
            out[icao] = Aircraft(
                icao=icao, flight=str(a.get("flight", "")).strip(), lat_deg=float(a["lat"]), lon_deg=float(a["lon"]),
                alt_m=alt, alt_source=src, gs_m_s=float(a["gs"]) * KT, track_deg=float(a["track"]),
                vrate_m_s=float(rate) * FT / 60, t_pos_ns=int((now_s - float(a.get("seen_pos", 0))) * NS),
                type=str(a.get("t", "")), reg=str(a.get("r", "")), category=str(a.get("category", "")),
                squawk=str(a.get("squawk", "")),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


class AdsbSource:
    """Reads aircraft.json (file or URL) on demand, at most every min_interval_s, and keeps a
    short trail of positions per aircraft."""

    def __init__(self, source: str, min_interval_s: float = 0.5):
        self.source = source
        self.min_interval_s = min_interval_s
        self._t_read = 0.0
        self._aircraft: dict[str, Aircraft] = {}
        self._trails: dict[str, deque] = {}
        self.note = "not read yet"
        self.ok = False
        self._lock = threading.Lock()  # mount-svc reads from its request and feeder threads

    def _fetch(self) -> dict:
        if self.source.startswith(("http://", "https://")):
            req = urllib.request.Request(self.source, headers={"User-Agent": "maxmount"})
            with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310 - configured LAN URL
                return json.loads(r.read())
        return json.loads(Path(self.source).read_text())

    def aircraft(self) -> dict[str, Aircraft]:
        with self._lock:
            return self._read()

    def _read(self) -> dict[str, Aircraft]:
        if time.monotonic() - self._t_read < self.min_interval_s:
            return self._aircraft
        self._t_read = time.monotonic()
        try:
            doc = self._fetch()
        except (OSError, ValueError) as e:
            self.ok, self.note = False, f"no ADS-B data from {self.source}: {e}"
            self._aircraft = {}
            return self._aircraft
        current = parse(doc)
        cutoff = time.time_ns() - TRAIL_S * NS
        for icao, a in current.items():
            tr = self._trails.setdefault(icao, deque())
            if not tr or tr[-1][0] != a.t_pos_ns:
                tr.append((a.t_pos_ns, a.lat_deg, a.lon_deg, a.alt_m))
            while tr and tr[0][0] < cutoff:
                tr.popleft()
            a.trail = list(tr)
        for icao in [k for k in self._trails if k not in current]:
            if not self._trails[icao] or self._trails[icao][-1][0] < cutoff:
                del self._trails[icao]
        age = time.time() - float(doc.get("now") or time.time())
        self.ok = True
        self.note = f"{len(current)} aircraft with positions ({len(doc.get('aircraft') or [])} seen), data {age:.1f} s old"
        self._aircraft = current
        return current

    def get(self, icao: str) -> Aircraft | None:
        return self.aircraft().get(icao.lower())


class AircraftTarget:
    """Az/el of an aircraft from the site, extrapolated from its latest ADS-B state. update()
    replaces the state (thread-safe enough: one reference swap)."""

    def __init__(self, site, aircraft: Aircraft):
        self.site = site
        self._site_m = astro.site_itrs_km(site) * 1000
        self._enu = astro.enu_basis(site)
        self.update(aircraft)

    def update(self, aircraft: Aircraft) -> None:
        p, v = aircraft.ecef_m()
        self._state = (p, v, aircraft.t_pos_ns)
        self.aircraft = aircraft
        self.name = aircraft.label

    def enu_m(self, t_ns):
        p, v, t0 = self._state
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        pos = p + np.outer((t - t0) / NS, v)
        return (pos - self._site_m) @ self._enu.T

    def observed(self, t_ns):
        """Geometric az/el (refraction over tens of km of air is small and ignored)."""
        scalar = np.ndim(t_ns) == 0
        az, el = astro.enu_to_azel(self.enu_m(t_ns))
        return (az[0], el[0]) if scalar else (az, el)

    def lonlat(self, t_ns):
        """Extrapolated longitude, latitude (deg) and height (m)."""
        p, v, t0 = self._state
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        pos = p + np.outer((t - t0) / NS, v)
        lon, lat, h = erfa.gc2gd(1, pos)
        return np.degrees(lon), np.degrees(lat), h


def closest_approach(target: AircraftTarget, t0_ns: int, horizon_s: float = 900, step_s: float = 2.0):
    """Over the next horizon_s of straight-line flight: time and range (m) of the closest
    approach, the elevation then, the highest elevation, and the peak angular rate (rad/s)."""
    t = np.arange(t0_ns, t0_ns + int(horizon_s * NS) + 1, int(step_s * NS), dtype=np.int64)
    enu = target.enu_m(t)
    rng = np.linalg.norm(enu, axis=-1)
    u = enu / rng[:, None]
    k = int(np.argmin(rng))
    el = np.arcsin(np.clip(u[:, 2], -1, 1))
    rate = np.arccos(np.clip(np.sum(u[1:] * u[:-1], axis=-1), -1, 1)) / step_s
    return {"t_ns": int(t[k]), "range_m": float(rng[k]), "el_rad": float(el[k]),
            "max_el_rad": float(el.max()), "max_rate_rad_s": float(rate.max()) if len(rate) else 0.0}


# -- simulator ---------------------------------------------------------------------------------
def simulate(site, t_s: float, n: int = 8, seed: int = 1) -> dict:
    """aircraft.json at time t_s with n synthetic aircraft on straight lines passing near the
    site (deterministic for a seed). Each passes the site every 20 minutes (from 10 min before
    its closest approach to 10 min after, then repeats)."""
    rng = np.random.default_rng(seed)
    lat0, lon0 = math.radians(site.latitude_deg), math.radians(site.longitude_deg)
    out = []
    for i in range(n):
        heading = rng.uniform(0, 360)
        miss = rng.uniform(-15000, 15000)              # closest approach distance, m
        speed = rng.uniform(110, 250)                  # m/s
        alt = rng.uniform(1500, 11500)                 # m
        vrate = rng.choice([0.0, 0.0, 6.0, -6.0])
        phase = rng.uniform(0, 1200)
        tt = (t_s + phase) % 1200 - 600                # s from closest approach: -10..+10 min
        h = math.radians(heading)
        # Local east/north offset from the site: along-track + perpendicular miss.
        e = math.sin(h) * speed * tt + math.cos(h) * miss
        nn = math.cos(h) * speed * tt - math.sin(h) * miss
        lat = lat0 + nn / 6378137.0
        lon = lon0 + e / (6378137.0 * math.cos(lat0))
        out.append({
            "hex": f"a{i:05x}", "flight": f"SIM{100 + i:<5}", "lat": round(math.degrees(lat), 6),
            "lon": round(math.degrees(lon), 6), "alt_geom": round((alt + vrate * tt) / FT),
            "alt_baro": round((alt + vrate * tt) / FT) - 150, "gs": round(speed / KT, 1), "track": round(heading, 1),
            "geom_rate": round(vrate / FT * 60), "seen_pos": 0.2, "seen": 0.1, "category": "A3",
            "t": "A320", "r": f"N{100 + i}SM", "squawk": f"{1200 + i}",
        })
    return {"now": t_s, "messages": int(t_s), "aircraft": out}


def run_simulator(site, path: str, n: int = 8, seed: int = 1, period_s: float = 1.0, stop=None) -> None:
    """Writes aircraft.json like readsb (atomically, once a second) until stop() is true."""
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    while not (stop and stop()):
        tmp.write_text(json.dumps(simulate(site, time.time(), n, seed)))
        tmp.replace(dst)
        time.sleep(period_s)
