# SPDX-License-Identifier: GPL-3.0-or-later
"""quicksat.mag parsing, RCS fallbacks, overrides and the magnitude model."""
import math

import numpy as np
import pytest

from maxmount_svc import satcat
from maxmount_svc.satcat import SatCatalog, parse_quicksat_mag, predicted_mag

# Real lines from quicksat.mag (CRLF and the header line included).
SAMPLE = (
    "00001 d Desig...  Name.......... Mag.  Sz1 Sz2 Sz3 RCS Comments 69900\r\n"
    "00005   58 2B     Vanguard 1      9.5  0.2 0.0 0.0 .11\r\n"
    "00127   61 15M    Trans 4A RkDbM                   .36\r\n"
    "20580   90 37B    HST             1.5 13.3 4.3 0.0  29 occ flare to -4\r\n"
    "25544   98 67A    ISS            -2.5              399\r\n"
    "48274   21 35A    CSS (TIANHE-1)\r\n"
    "A0685   24 134M   USA 667\r\n"
    "A0700   24 134X   TEST ALPHA5     6.0  3.0 1.0 0.0 1.0\r\n"
)


def test_parse_columns():
    d = parse_quicksat_mag(SAMPLE)
    assert d["5"].std_mag == 9.5 and d["5"].size_m == (0.2,) and d["5"].rcs_m2 == pytest.approx(0.11)
    hst = d["20580"]
    assert hst.std_mag == 1.5 and hst.size_m == (13.3, 4.3) and hst.rcs_m2 == 29 and hst.comment == "occ flare to -4"
    assert d["25544"].std_mag == -2.5 and d["25544"].size_m is None and d["25544"].rcs_m2 == 399
    assert "48274" not in d and "A0685" not in d  # no data at all
    assert d["A0700"].std_mag == 6.0                 # Alpha-5 catalog numbers
    assert "1" not in d                              # header


def test_rcs_fallbacks_and_overrides(tmp_path):
    ov = tmp_path / "satellites.yaml"
    ov.write_text("25544: {size_m: [109, 73], note: ISS}\n48274: {std_mag: -1.0}\n")
    cat = SatCatalog.from_text(SAMPLE, overrides=ov)
    rk = cat.get("00127")  # RCS only: both estimated
    assert rk.mag_estimated and rk.std_mag == pytest.approx(satcat.RCS_MAG0 - 2.5 * math.log10(0.36), abs=0.05)
    assert rk.size_estimated and rk.size_m[0] == pytest.approx(2.0 * math.sqrt(0.36), abs=0.05)
    iss = cat.get("25544")
    assert iss.std_mag == -2.5 and not iss.mag_estimated and iss.size_m == (109, 73) and not iss.size_estimated
    assert cat.get("48274").std_mag == -1.0 and cat.get("48274").size_m is None  # from the overrides only
    assert cat.get("99999") is None
    assert cat.get(" 25544").std_mag == -2.5


def test_repo_overrides_parse():
    cat = SatCatalog.from_text(SAMPLE, overrides=satcat.OVERRIDES)
    assert cat.get("25544").size_m[0] > 100


def test_predicted_magnitude():
    z = math.pi / 2
    assert predicted_mag(2.0, 1000, 0.0, z) == pytest.approx(2.0)                       # the standard
    assert predicted_mag(2.0, 2000, 0.0, z) == pytest.approx(2.0 + 5 * math.log10(2))   # range
    assert predicted_mag(2.0, 1000, math.pi / 2, z) == pytest.approx(2.0 + 2.5 * math.log10(2))  # 90 deg: ~0.75 fainter
    assert predicted_mag(2.0, 1000, 0.0, math.radians(30)) == pytest.approx(2.0 + satcat.EXTINCTION)  # airmass 2
    m = predicted_mag(0.0, np.array([500, 1000]), np.array([0.0, 0.0]), np.array([z, z]))
    assert m[0] < m[1]
