# SPDX-License-Identifier: GPL-3.0-or-later
"""Time scales and topocentric geometry on ERFA (pyerfa).

Conventions: times are int64 nanoseconds since the Unix epoch (UTC, as CLOCK_REALTIME);
angles are radians; azimuth is measured from north through east; "observed" positions
include atmospheric refraction.
"""
from __future__ import annotations

import erfa
import numpy as np

from .config import Site

NS_PER_DAY = 86_400_000_000_000
UNIX_EPOCH_JD = 2440587.5
C_KM_S = 299_792.458


def utc_jd(t_ns) -> tuple[np.ndarray, np.ndarray]:
    """UTC as an ERFA two-part quasi Julian date (leap-second days are ignored)."""
    t = np.asarray(t_ns, dtype=np.int64)
    days, rem = np.divmod(t, NS_PER_DAY)
    return UNIX_EPOCH_JD + days.astype(np.float64), rem.astype(np.float64) / NS_PER_DAY


def tt_jd(t_ns) -> tuple[np.ndarray, np.ndarray]:
    u1, u2 = utc_jd(t_ns)
    a1, a2 = erfa.utctai(u1, u2)
    return erfa.taitt(a1, a2)


def ut1_jd(t_ns, dut1_s: float) -> tuple[np.ndarray, np.ndarray]:
    u1, u2 = utc_jd(t_ns)
    return erfa.utcut1(u1, u2, dut1_s)


def _site_args(site: Site):
    return (
        np.radians(site.longitude_deg),
        np.radians(site.latitude_deg),
        site.height_m,
        0.0,  # polar motion xp, yp: < 0.5", absorbed by the pointing model
        0.0,
        site.pressure_hpa,
        site.temperature_c,
        site.humidity,
        site.wavelength_um,
    )


def observed_from_icrs(t_ns, site: Site, ra, dec, pmra=0.0, pmdec=0.0, parallax_as=0.0, rv_km_s=0.0):
    """ICRS catalog position (epoch J2000, proper motion in RA as dRA/dt, rad/yr) to
    observed azimuth and elevation, including aberration, precession-nutation and refraction."""
    u1, u2 = utc_jd(t_ns)
    elong, phi, hm, xp, yp, phpa, tc, rh, wl = _site_args(site)
    aob, zob, _hob, _dob, _rob, _eo = erfa.atco13(
        ra, dec, pmra, pmdec, parallax_as, rv_km_s, u1, u2, site.dut1_s, elong, phi, hm, xp, yp, phpa, tc, rh, wl
    )
    return np.mod(aob, 2 * np.pi), np.pi / 2 - zob


def refraction_dz(zenith_distance, site: Site):
    """Refraction (rad) to subtract from a geometric zenith distance. ERFA's two-term model;
    good above ~15 deg elevation, approximate down to the horizon mask."""
    if site.pressure_hpa <= 0:
        return np.zeros_like(np.asarray(zenith_distance, dtype=float))
    refa, refb = erfa.refco(site.pressure_hpa, site.temperature_c, site.humidity, site.wavelength_um)
    tz = np.tan(np.minimum(zenith_distance, np.radians(87.0)))
    return refa * tz + refb * tz**3


def site_itrs_km(site: Site) -> np.ndarray:
    xyz_m = erfa.gd2gc(1, np.radians(site.longitude_deg), np.radians(site.latitude_deg), site.height_m)
    return np.asarray(xyz_m) / 1000.0


def teme_to_pef(r_teme_km, t_ns, dut1_s: float) -> np.ndarray:
    """TEME (SGP4 output frame) to pseudo-Earth-fixed: rotation by GMST (IAU 1982), as in
    Vallado et al. 2006. Polar motion (< 15 m on the ground) is neglected."""
    r = np.asarray(r_teme_km, dtype=float)
    g = erfa.gmst82(*ut1_jd(t_ns, dut1_s))
    c, s = np.cos(g), np.sin(g)
    return np.stack([c * r[..., 0] + s * r[..., 1], -s * r[..., 0] + c * r[..., 1], r[..., 2]], axis=-1)


def enu_basis(site: Site) -> np.ndarray:
    """Rows: east, north, up unit vectors in the Earth-fixed frame (geodetic vertical)."""
    lat, lon = np.radians(site.latitude_deg), np.radians(site.longitude_deg)
    sl, cl, sp, cp = np.sin(lon), np.cos(lon), np.sin(lat), np.cos(lat)
    return np.array([[-sl, cl, 0.0], [-sp * cl, -sp * sl, cp], [cp * cl, cp * sl, sp]])


def enu_to_azel(enu):
    e, n, u = enu[..., 0], enu[..., 1], enu[..., 2]
    return np.mod(np.arctan2(e, n), 2 * np.pi), np.arctan2(u, np.hypot(e, n))


def azel_to_enu(az, el):
    ce = np.cos(el)
    return np.stack([ce * np.sin(az), ce * np.cos(az), np.sin(el)], axis=-1)


def sun_observed(t_ns, site: Site):
    """Observed az/el of the Sun (geocentric direction; parallax is irrelevant for avoidance)."""
    t1, t2 = tt_jd(t_ns)
    pvh, _pvb = erfa.epv00(t1, t2)  # heliocentric Earth position/velocity, au (structured 'pv')
    ra, dec = erfa.c2s(-pvh["p"])
    return observed_from_icrs(t_ns, site, ra, dec)


def separation(az1, el1, az2, el2):
    return erfa.seps(az1, el1, az2, el2)
