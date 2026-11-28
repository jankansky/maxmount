# SPDX-License-Identifier: GPL-3.0-or-later
"""Target providers: observed (refracted) topocentric az/el as a function of UTC time."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import erfa
import numpy as np
from sgp4.api import SGP4_ERRORS, Satrec

from . import astro
from .config import Site


class Target(Protocol):
    name: str

    def observed(self, t_ns) -> tuple[np.ndarray, np.ndarray]:
        """Observed azimuth and elevation (rad) at the given times (int64 ns, UTC)."""
        ...


@dataclass
class FixedTarget:
    az: float
    el: float
    name: str = "fixed"

    def observed(self, t_ns):
        n = np.shape(np.asarray(t_ns))
        return np.full(n, self.az), np.full(n, self.el)


@dataclass
class SiderealTarget:
    """ICRS catalog position. pmra is dRA/dt in rad/yr (ERFA convention, not cos(dec)-scaled)."""

    site: Site
    ra: float
    dec: float
    pmra: float = 0.0
    pmdec: float = 0.0
    parallax_as: float = 0.0
    rv_km_s: float = 0.0
    name: str = "sidereal"

    def observed(self, t_ns):
        return astro.observed_from_icrs(
            t_ns, self.site, self.ra, self.dec, self.pmra, self.pmdec, self.parallax_as, self.rv_km_s
        )


class SgpError(RuntimeError):
    pass


@dataclass
class SatelliteTarget:
    """Satellite from a TLE (Vallado's SGP4), with live corrections for TLE errors:

    time_offset_s     along-track: the satellite is predicted at t + time_offset_s
    cross_track_rad   offset perpendicular to the apparent motion (positive = left of the motion)
    """

    site: Site
    line1: str
    line2: str
    name: str = "satellite"
    time_offset_s: float = 0.0
    cross_track_rad: float = 0.0
    _sat: Satrec = field(init=False, repr=False)

    def __post_init__(self):
        self._sat = Satrec.twoline2rv(self.line1.strip(), self.line2.strip())
        self._site_km = astro.site_itrs_km(self.site)
        self._enu = astro.enu_basis(self.site)

    @property
    def epoch_ns(self) -> int:
        jd = self._sat.jdsatepoch + self._sat.jdsatepochF
        return int(round((jd - astro.UNIX_EPOCH_JD) * astro.NS_PER_DAY))

    def _pef_km(self, t_ns):
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        jd1, jd2 = astro.utc_jd(t)
        err, r, _v = self._sat.sgp4_array(jd1, jd2)
        if np.any(err != 0):
            code = int(err[err != 0][0])
            raise SgpError(f"{self.name}: SGP4 error {code}: {SGP4_ERRORS.get(code, 'unknown')}")
        return astro.teme_to_pef(r, t, self.site.dut1_s)

    def geometric_enu_km(self, t_ns):
        """Topocentric position (east, north, up), corrected for light time."""
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64)) + np.int64(round(self.time_offset_s * 1e9))
        rel = self._pef_km(t) - self._site_km
        for _ in range(2):  # light time: the light left the satellite range / c earlier
            dt = np.linalg.norm(rel, axis=-1) / astro.C_KM_S
            rel = self._pef_km(t - (dt * 1e9).astype(np.int64)) - self._site_km
        # The Earth rotates while the light travels; at LEO ranges this is a few metres.
        return rel @ self._enu.T

    def sunlit(self, t_ns) -> np.ndarray:
        """True where the satellite is outside the Earth's shadow (cylindrical shadow model;
        good to a few seconds at shadow entry and exit)."""
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        jd1, jd2 = astro.utc_jd(t)
        _err, r, _v = self._sat.sgp4_array(jd1, jd2)
        r = np.asarray(r)
        t1, t2 = astro.tt_jd(t)
        pvh, _ = erfa.epv00(t1, t2)
        sun = -np.asarray(pvh["p"])  # geocentric Sun direction (GCRS ~ TEME at this accuracy)
        sun = sun / np.linalg.norm(sun, axis=-1, keepdims=True)
        along = np.sum(r * sun, axis=-1)
        perp = np.linalg.norm(r - along[..., None] * sun, axis=-1)
        return (along > 0) | (perp > 6378.137)

    def geometry(self, t_ns: int) -> dict:
        """Range, range rate and altitude (km, km/s) at t."""
        t = np.array([t_ns, t_ns + 500_000_000], dtype=np.int64)
        enu = self.geometric_enu_km(t)
        rng = np.linalg.norm(enu, axis=-1)
        jd1, jd2 = astro.utc_jd(t[:1])
        _err, r, _v = self._sat.sgp4_array(jd1, jd2)
        return {
            "range_km": float(rng[0]),
            "range_rate_km_s": float((rng[1] - rng[0]) / 0.5),
            "altitude_km": float(np.linalg.norm(np.asarray(r)[0]) - 6378.137),
        }

    def observed(self, t_ns):
        scalar = np.ndim(t_ns) == 0
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        u = _unit(self.geometric_enu_km(t))
        if self.cross_track_rad:
            u_later = _unit(self.geometric_enu_km(t + 500_000_000))
            u = _cross_track(u, u_later, self.cross_track_rad)
        az, el = astro.enu_to_azel(u)
        el = el + astro.refraction_dz(np.pi / 2 - el, self.site)
        return (az[0], el[0]) if scalar else (az, el)


def _unit(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _cross_track(u, u_later, offset):
    """Rotates each direction by `offset` perpendicular to its apparent motion (positive = to
    the left of the motion as seen by the observer, i.e. towards u x motion)."""
    left = np.cross(u, u_later - u)
    norm = np.linalg.norm(left, axis=-1, keepdims=True)
    left = np.where(norm > 0, left / np.where(norm > 0, norm, 1.0), 0.0)
    return u * math.cos(offset) + left * math.sin(offset)


class Sampled:
    """A slowly moving target (star, planet, Moon) sampled exactly every step_s over
    [t0, t1] and interpolated in between: a cubic (4-point Lagrange) through the observed
    east/north/up unit vectors, renormalised. Planning evaluates a fixed-sky target once a
    second for hours, and ERFA's full ICRS -> observed transform per sample takes ~0.7 ms on
    a Pi 4 (30 s for a 4 h Saturn track, past the request timeout). The sky turns 0.25 deg in
    60 s, so the interpolation error is ~1e-10 rad; refraction near the horizon is just as
    smooth on that scale. Times outside the grid go to the wrapped target."""

    def __init__(self, target, t0_ns: int, t1_ns: int, step_s: float = 60.0):
        self.target, self.name = target, target.name
        self._step = int(step_s * 1_000_000_000)
        self._t0 = int(t0_ns) - self._step
        n = int((int(t1_ns) - self._t0) // self._step) + 3  # one sample of margin each side
        t = self._t0 + self._step * np.arange(n, dtype=np.int64)
        self._t1 = int(t[-1])
        self._v = astro.azel_to_enu(*target.observed(t))

    def __getattr__(self, name):
        return getattr(self.target, name)

    def observed(self, t_ns):
        scalar = np.ndim(t_ns) == 0
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        inside = (t >= self._t0 + self._step) & (t <= self._t1 - self._step)
        if not inside.all():
            if not inside.any():
                return self.target.observed(t_ns)
            az, el = self.target.observed(t)
            az_i, el_i = self._interp(t[inside])
            az[inside], el[inside] = az_i, el_i
        else:
            az, el = self._interp(t)
        return (float(az[0]), float(el[0])) if scalar else (az, el)

    def _interp(self, t):
        x = (t - self._t0) / self._step                    # grid index, float
        i = np.clip(np.floor(x).astype(np.int64), 1, len(self._v) - 3)
        u = (x - i)[:, None]                               # in [0, 1): between samples i and i+1
        w = (-u * (u - 1) * (u - 2) / 6, (u + 1) * (u - 1) * (u - 2) / 2,
             -(u + 1) * u * (u - 2) / 2, (u + 1) * u * (u - 1) / 6)
        v = w[0] * self._v[i - 1] + w[1] * self._v[i] + w[2] * self._v[i + 1] + w[3] * self._v[i + 2]
        return astro.enu_to_azel(v / np.linalg.norm(v, axis=-1, keepdims=True))
