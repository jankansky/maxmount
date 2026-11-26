# SPDX-License-Identifier: GPL-3.0-or-later
"""SOCRATES CSV parsing, OMM element sets, and screening for conjunctions visible from the site."""
import numpy as np
import pytest

from maxmount_svc import planner, socrates
from maxmount_svc.targets import SatelliteTarget

from .conftest import ISS

HEADER = "NORAD_CAT_ID_1,OBJECT_NAME_1,DSE_1,NORAD_CAT_ID_2,OBJECT_NAME_2,DSE_2,TCA,TCA_RANGE,TCA_RELATIVE_SPEED,MAX_PROB,DILUTION\n"

# The ISS (2008) and a made-up object 600 m behind it along the orbit (mean anomaly +0.005 deg).
PAIR_OMM = (
    "OBJECT_NAME,OBJECT_ID,EPOCH,MEAN_MOTION,ECCENTRICITY,INCLINATION,RA_OF_ASC_NODE,ARG_OF_PERICENTER,MEAN_ANOMALY,"
    "EPHEMERIS_TYPE,CLASSIFICATION_TYPE,NORAD_CAT_ID,ELEMENT_SET_NO,REV_AT_EPOCH,BSTAR,MEAN_MOTION_DOT,MEAN_MOTION_DDOT\n"
    "ISS (ZARYA),1998-067A,2008-09-20T12:25:40.104192,15.72125391,.0006703,51.6416,247.4627,130.5360,325.0288,0,U,25544,292,56353,-.11606E-4,-.00002182,0\n"
    "TEST DEBRIS,1998-067ZZ,2008-09-20T12:25:40.104192,15.72125391,.0006703,51.6416,247.4627,130.5360,325.0338,0,U,99001,1,56353,-.11606E-4,-.00002182,0\n"
)


def _utc(t_ns: int) -> str:
    return str(np.datetime64(int(t_ns), "ns"))[:23].replace("T", " ")


def iss_conjunctions(site, mask_deg):
    """A conjunction at the ISS's highest point in its first whole pass after epoch, one while
    it is below the horizon, and one 3 km miss; returns (csv text, the pass)."""
    sat = SatelliteTarget(site, *ISS)
    ps = planner.find_passes(sat, sat.epoch_ns, sat.epoch_ns + 24 * 3600 * 10**9, np.radians(mask_deg))
    p = next(q for q in ps if q.rise_ns > sat.epoch_ns)
    below = p.rise_ns - 20 * 60 * 10**9
    text = HEADER + "".join([
        f"25544,ISS (ZARYA) [+],0.5,99001,TEST DEBRIS [-],0.5,{_utc(p.culmination_ns)},0.600,0.050,1.0E-04,0.100\n",
        f"25544,ISS (ZARYA) [+],0.5,99001,TEST DEBRIS [-],0.5,{_utc(below)},0.400,12.0,1.0E-05,0.200\n",
        f"99001,TEST DEBRIS [-],0.5,25544,ISS (ZARYA) [+],0.5,{_utc(p.culmination_ns + 60 * 10**9)},3.000,9.5,1.0E-06,0.300\n",
    ])
    return text, p


def test_parse_csv():
    rows = socrates.parse_csv(HEADER + "60485,TOMORROW-S2 [+],7.888,66118,STARLINK-35468 [+],8.108,2026-12-01 08:54:06.014,"
                              "0.008,14.772,1.000E+00,0.000\nbad,row\n")
    assert len(rows) == 1
    c = rows[0]
    assert (c.cat1, c.name2, c.range_km, c.speed_km_s, c.max_prob) == ("60485", "STARLINK-35468 [+]", 0.008, 14.772, 1.0)
    assert c.tca_ns == int(np.datetime64("2026-12-01T08:54:06.014", "ns").astype(np.int64))
    with pytest.raises(ValueError):
        socrates.parse_csv("<html>not found</html>")


def test_omm_to_tles():
    t = socrates.omm_to_tles(PAIR_OMM)
    assert set(t) == {"25544", "99001"}
    name, l1, l2 = t["25544"]
    assert name == "ISS (ZARYA)" and l1.startswith("1 25544") and l2.split()[6] == "325.0288"


def test_screen_keeps_what_the_site_can_see(site):
    text, p = iss_conjunctions(site, 10.0)
    conj = socrates.parse_csv(text)
    tles = {"25544": ("ISS (ZARYA)",) + ISS}
    t0, t1 = p.rise_ns - 3600 * 10**9, p.set_ns + 3600 * 10**9
    out = socrates.screen(conj, tles, site, t0, t1, 1.0, 10.0)
    assert len(out) == 1  # the one below the horizon and the 3 km miss are dropped
    c = out[0]
    assert c["primary"] == "25544" and c["cat2"] == "99001"
    assert c["el_deg"] == pytest.approx(np.degrees(p.max_el), abs=0.2)  # geometric vs observed elevation
    assert 300 < c["obs_range_km"] < 2600 and 0 <= c["phase_rad"] <= np.pi
    assert isinstance(c["sunlit"], bool) and -90 <= c["sun_el_deg"] <= 90
    # The 3 km miss appears with a larger limit; the secondary is the primary when it has elements.
    out = socrates.screen(conj, tles, site, t0, t1, 5.0, 10.0)
    assert len(out) == 2 and all(c["primary"] == "25544" for c in out)
    assert socrates.screen(conj, tles, site, t1, t1 + 10**9, 5.0, 10.0) == []   # outside the window
    assert socrates.screen(conj, {}, site, t0, t1, 5.0, 10.0) == []             # no elements for either
