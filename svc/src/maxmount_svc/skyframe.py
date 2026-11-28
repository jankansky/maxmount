# SPDX-License-Identifier: GPL-3.0-or-later
"""Sky chart astronomy: the ICRS -> topocentric rotation the browser uses to place catalogue
objects, the Sun, Moon and planets (ERFA), rise/transit/set, and the "tonight" object list.

Frame. frame_matrix(t) is the rotation that best maps ICRS unit vectors to the observer's
geometric east/north/up frame (precession-nutation, Earth rotation, polar motion ignored),
fitted to erfa.atco13 at a few directions. A rotation cannot hold annual aberration (up to
20.5", not a rigid rotation), so it is good to ~20" across the sky: plenty for a chart. Tracking
uses the full ERFA path (targets.py), not this.

Solar system. Planets from erfa.plan94 (heliocentric), the Earth from erfa.epv00 and the Moon
from erfa.moon98, with light time; "astrometric" positions (ICRS axes, as JPL Horizons gives).
plan94 is good to arcseconds (inner planets) to tens of arcseconds (outer) this century; moon98
to ~10". Topocentric positions subtract the observer's geocentric position: ~1 deg of parallax
for the Moon, up to ~20" for Mars and Venus.
"""
from __future__ import annotations

import math
import time

import erfa
import numpy as np

from . import astro, skycat
from .config import Site

NS = 1_000_000_000
DEG = math.pi / 180
AU_KM = 149_597_870.7
C_AU_DAY = 173.1446326846693  # speed of light, au/day
DAY_NS = 86400 * NS

BODIES = ("sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")
_PLAN94 = {"mercury": 1, "venus": 2, "mars": 4, "jupiter": 5, "saturn": 6, "uranus": 7, "neptune": 8}
RADIUS_KM = {"sun": 695_700.0, "moon": 1737.4, "mercury": 2439.7, "venus": 6051.8, "mars": 3396.2,
             "jupiter": 71_492.0, "saturn": 60_268.0, "uranus": 25_559.0, "neptune": 24_764.0}
# V magnitude: H + 5 log10(r delta) + phase law (degrees), Astronomical Almanac / Meeus ch. 41.
_MAG = {
    "mercury": lambda i: -0.42 + 0.0380 * i - 0.000273 * i**2 + 0.000002 * i**3,
    "venus": lambda i: -4.40 + 0.0009 * i + 0.000239 * i**2 - 0.00000065 * i**3,
    "mars": lambda i: -1.52 + 0.016 * i,
    "jupiter": lambda i: -9.40 + 0.005 * i,
    "saturn": lambda i: -8.88 + 0.0 * i,  # rings ignored (they add up to ~-0.5 mag)
    "uranus": lambda i: -7.19 + 0.0 * i,
    "neptune": lambda i: -6.87 + 0.0 * i,
}

# Fit directions: spread over the sphere so the rotation is well determined.
_FIT = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [-1, 0, 0], [0, -1, 0], [0, 0, -1],
                 [1, 1, 1], [-1, 1, -1], [1, -1, -1], [-1, -1, 1]], dtype=float)
_FIT /= np.linalg.norm(_FIT, axis=1)[:, None]
_FIT_RA = np.arctan2(_FIT[:, 1], _FIT[:, 0])
_FIT_DEC = np.arcsin(_FIT[:, 2])


def _jd(t_ns):
    """(UTC two-part JD, TT two-part JD) for times in ns."""
    t = np.asarray(t_ns, dtype=np.int64)
    return astro.utc_jd(t), astro.tt_jd(t)


def _geometric_azel(t_ns, site: Site, ra, dec):
    """atco13 without refraction (pressure 0): geometric topocentric az/el of ICRS directions.
    t_ns and ra/dec broadcast."""
    (u1, u2) = astro.utc_jd(np.asarray(t_ns, dtype=np.int64))
    elong, phi = math.radians(site.longitude_deg), math.radians(site.latitude_deg)
    aob, zob, *_ = erfa.atco13(ra, dec, 0.0, 0.0, 0.0, 0.0, u1, u2, site.dut1_s, elong, phi, site.height_m,
                               0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return aob, math.pi / 2 - zob


def frame_matrices(t_ns, site: Site) -> np.ndarray:
    """Rotation matrices R[n, 3, 3] with enu = R @ icrs_unit_vector, one per time (Kabsch fit)."""
    t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
    az, el = _geometric_azel(t[:, None], site, _FIT_RA[None, :], _FIT_DEC[None, :])
    enu = astro.azel_to_enu(az, el)                      # (n, k, 3)
    h = np.einsum("nki,kj->nij", enu, _FIT)              # sum_k enu_k x icrs_k^T
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(u @ vt))
    u[:, :, -1] *= d[:, None]
    return u @ vt


def frame_matrix(t_ns: int, site: Site) -> np.ndarray:
    return frame_matrices(np.int64(t_ns), site)[0]


def refraction_constants(site: Site) -> tuple[float, float]:
    """ERFA refraction constants (A, B), rad: dz = A tan z + B tan^3 z."""
    if site.pressure_hpa <= 0:
        return 0.0, 0.0
    a, b = erfa.refco(site.pressure_hpa, site.temperature_c, site.humidity, site.wavelength_um)
    return float(a), float(b)


def refract(el, site: Site):
    """Geometric elevation (rad) -> observed (refraction added), clamped near the horizon."""
    el = np.asarray(el, dtype=float)
    return el + astro.refraction_dz(np.pi / 2 - el, site) * (el > -math.radians(2))


# -- solar system --------------------------------------------------------------------------------
def _earth_helio(tt1, tt2):
    pvh, _pvb = erfa.epv00(tt1, tt2)
    return pvh["p"]  # au, ICRS axes


def geocentric(body: str, t_ns) -> np.ndarray:
    """Astrometric geocentric position (au, ICRS axes) with light time; t_ns scalar or array."""
    tt1, tt2 = astro.tt_jd(np.asarray(t_ns, dtype=np.int64))
    tt1, tt2 = np.asarray(tt1, dtype=float), np.asarray(tt2, dtype=float)
    if body == "moon":
        pv = erfa.moon98(tt1, tt2)
        p, v = pv["p"], pv["v"]
        tau = np.linalg.norm(p, axis=-1, keepdims=True) / C_AU_DAY
        return p - v * tau
    earth = _earth_helio(tt1, tt2)
    if body == "sun":
        return -earth
    n = _PLAN94[body]
    tau = np.zeros(np.shape(tt1))
    for _ in range(3):
        pv = erfa.plan94(tt1, tt2 - tau, n)
        g = pv["p"] - earth
        tau = np.linalg.norm(g, axis=-1) / C_AU_DAY
    return g


def _observer_enu_au(site: Site) -> np.ndarray:
    return astro.enu_basis(site) @ astro.site_itrs_km(site) / AU_KM


def body_topocentric(body: str, t_ns, site: Site):
    """Geometric topocentric (az, el, distance au) of a body: the geocentric direction through
    ERFA (aberration, precession-nutation, Earth rotation), then the observer's offset from the
    geocentre (parallax)."""
    g = geocentric(body, t_ns)
    dist = np.linalg.norm(g, axis=-1)
    ra, dec = np.arctan2(g[..., 1], g[..., 0]), np.arcsin(g[..., 2] / dist)
    az, el = _geometric_azel(t_ns, site, ra, dec)
    v = astro.azel_to_enu(az, el) * dist[..., None] - _observer_enu_au(site)
    d = np.linalg.norm(v, axis=-1)
    az2, el2 = astro.enu_to_azel(v / d[..., None])
    return az2, el2, d


class BodyTarget:
    """A solar-system body as a mount-svc target: observed az/el (refraction included)."""

    def __init__(self, site: Site, body: str, name: str = ""):
        if body not in BODIES:
            raise ValueError(f"unknown body '{body}' (one of {', '.join(BODIES)})")
        self.site, self.body = site, body
        self.name = name or body.capitalize()

    def observed(self, t_ns):
        scalar = np.ndim(t_ns) == 0
        az, el, _d = body_topocentric(self.body, np.atleast_1d(np.asarray(t_ns, dtype=np.int64)), self.site)
        el = refract(el, self.site)
        return (float(az[0]), float(el[0])) if scalar else (az, el)


def _bright_limb_pa(ra, dec, ra_sun, dec_sun) -> float:
    """Position angle (deg, north through east) of the midpoint of the bright limb (Meeus 48.5)."""
    y = math.cos(dec_sun) * math.sin(ra_sun - ra)
    x = math.sin(dec_sun) * math.cos(dec) - math.cos(dec_sun) * math.sin(dec) * math.cos(ra_sun - ra)
    return math.degrees(math.atan2(y, x)) % 360


def solar_system(t_ns: int, site: Site, R: np.ndarray | None = None) -> list[dict]:
    """Sun, Moon and planets now: topocentric directions expressed in ICRS through the chart's
    frame matrix (so they land where the chart draws them), with magnitude, apparent diameter,
    illuminated fraction and (Moon) bright-limb position angle."""
    R = frame_matrix(t_ns, site) if R is None else R
    t = np.int64(t_ns)
    sun_g = geocentric("sun", t)
    out = []
    for body in BODIES:
        az, el, d = body_topocentric(body, np.atleast_1d(t), site)
        v = R.T @ astro.azel_to_enu(az[0], el[0])
        ra, dec = math.atan2(v[1], v[0]) % (2 * math.pi), math.asin(max(-1.0, min(1.0, v[2])))
        g = geocentric(body, t)
        delta = float(np.linalg.norm(g))
        item = {"body": body, "name": body.capitalize(), "ra_deg": math.degrees(ra), "dec_deg": math.degrees(dec),
                "az_deg": math.degrees(float(az[0])), "el_deg": math.degrees(float(refract(el[0], site))),
                "dist_au": float(d[0]),
                "diameter_arcsec": 2 * math.degrees(math.asin(min(1.0, RADIUS_KM[body] / (float(d[0]) * AU_KM)))) * 3600}
        if body == "sun":
            item.update(mag=-26.74, phase=1.0)
        else:
            helio = g - sun_g                    # body - sun, au (geocentric frame, same axes)
            r = float(np.linalg.norm(helio))
            cos_i = float(np.dot(helio, g) / (r * delta))   # phase angle at the body: sun-body-earth
            i = math.degrees(math.acos(max(-1.0, min(1.0, cos_i))))
            item["phase"] = (1 + math.cos(math.radians(i))) / 2
            if body == "moon":
                item["mag"] = -12.73 + 0.026 * i + 4e-9 * i**4
                sra, sdec = math.atan2(sun_g[1], sun_g[0]), math.asin(sun_g[2] / np.linalg.norm(sun_g))
                gra, gdec = math.atan2(g[1], g[0]), math.asin(g[2] / delta)
                item["bright_limb_pa_deg"] = _bright_limb_pa(gra, gdec, sra, sdec)
            else:
                item["mag"] = _MAG[body](i) + 5 * math.log10(r * delta)
        item["mag"] = round(item["mag"], 2)
        out.append(item)
    return out


# -- rise / transit / set ------------------------------------------------------------------------
def _crossings(t, alt, level):
    """Times where alt crosses level upwards and downwards (linear interpolation)."""
    above = alt >= level
    idx = np.flatnonzero(above[1:] != above[:-1])
    ups, downs = [], []
    for i in idx:
        f = (level - alt[i]) / (alt[i + 1] - alt[i])
        tc = int(t[i] + f * (t[i + 1] - t[i]))
        (ups if above[i + 1] else downs).append(tc)
    return ups, downs


def events(t, alt, level) -> dict:
    """Rise, transit (highest point) and set within the sampled window, at elevation `level`."""
    ups, downs = _crossings(t, alt, level)
    k = int(np.argmax(alt))
    if 0 < k < len(alt) - 1:  # parabolic refinement of the highest point
        a, b, c = alt[k - 1], alt[k], alt[k + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
        t_max, alt_max = int(t[k] + off * (t[1] - t[0])), float(b - 0.25 * (a - c) * off)
    else:
        t_max, alt_max = int(t[k]), float(alt[k])
    state = "circumpolar" if alt.min() >= level else "never rises" if alt.max() < level else "rises and sets"
    return {"rise_ns": ups[0] if ups else None, "set_ns": downs[0] if downs else None,
            "transit_ns": t_max, "transit_alt_deg": math.degrees(alt_max), "state": state, "up_now": bool(alt[0] >= level)}


def time_grid(t0_ns: int, hours: float = 24, step_s: float = 300) -> np.ndarray:
    return np.arange(t0_ns, t0_ns + int(hours * 3600 * NS) + 1, int(step_s * NS), dtype=np.int64)


def fixed_altitudes(Rs: np.ndarray, ra_deg, dec_deg) -> np.ndarray:
    """Geometric altitude (rad) of ICRS directions over a stack of frame matrices: (n_obj, n_t)."""
    ra, dec = np.radians(np.atleast_1d(ra_deg)), np.radians(np.atleast_1d(dec_deg))
    v = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)  # (m, 3)
    up = Rs[:, 2, :]                                                                           # (n, 3)
    return np.arcsin(np.clip(v @ up.T, -1, 1))


def darkness(t: np.ndarray, sun_alt: np.ndarray, limit_deg: float):
    """(start_ns, end_ns) of the dark period (Sun below limit) now or next in the window, or None."""
    dark = sun_alt < math.radians(limit_deg)
    if not dark.any():
        return None
    i0 = int(np.argmax(dark))
    i1 = i0 + int(np.argmax(~dark[i0:])) if (~dark[i0:]).any() else len(t) - 1
    return int(t[i0]), int(t[i1])


# -- catalogue helpers for the chart's ops -------------------------------------------------------
def search(query: str, limit: int = 20) -> list[dict]:
    q = "".join(query.lower().split())
    hits = [{"kind": "body", "body": b, "name": b.capitalize()} for b in BODIES if q and b.startswith(q)]
    return (hits + skycat.find(query, limit))[:limit]


class Cache:
    """Frame stacks are cheap but not free; the chart polls once a second."""

    def __init__(self):
        self._grid: tuple[int, int, np.ndarray, np.ndarray] | None = None

    def grid(self, site: Site, hours: float = 24, step_s: float = 300):
        """(t, R) over the next `hours`, recomputed each minute."""
        now = time.time_ns()
        key = now // (60 * NS)
        if self._grid is None or self._grid[0] != key or self._grid[1] != hours:
            t = time_grid(now, hours, step_s)
            self._grid = (key, hours, t, frame_matrices(t, site))
        return self._grid[2], self._grid[3]
