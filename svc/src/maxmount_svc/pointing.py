# SPDX-License-Identifier: GPL-3.0-or-later
"""Pointing model: corrections from ideal mechanical axis angles to commanded axis angles.

Terms follow TPOINT's alt-az set, written on mechanical angles (A, E) so they stay continuous
when the elevation axis goes past the zenith (E in 0..180 deg). Coefficients are in arcseconds.
The sign conventions are defined here and will be used unchanged by the fitter.

    IA    azimuth index              dA += IA
    IE    elevation index            dE += IE
    CA    collimation error          dA += CA / cos E
    NPAE  axes non-perpendicularity  dA += NPAE tan E
    AN    azimuth axis tilt north    dA -= AN sin A tan E,  dE -= AN cos A
    AW    azimuth axis tilt west     dA -= AW cos A tan E,  dE += AW sin A
    TF    tube flexure               dE += TF cos E
    BE    flip-side elevation index  dE += BE when E > 90 deg (mirror/flip-dependent)

Site terms (IA, IE, AN, AW) are refit at each new site; structural terms persist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

ARCSEC = np.pi / (180 * 3600)
TERMS = ("IA", "IE", "CA", "NPAE", "AN", "AW", "TF", "BE")
_MIN_COS = np.cos(np.radians(89.0))  # cap 1/cos E and tan E within 1 deg of the zenith


@dataclass
class PointingModel:
    coefficients: dict[str, float] = field(default_factory=dict)  # arcsec

    def __post_init__(self):
        unknown = set(self.coefficients) - set(TERMS)
        if unknown:
            raise ValueError(f"unknown pointing terms: {sorted(unknown)}")

    @classmethod
    def load(cls, path: str | Path | None) -> PointingModel:
        if path is None:
            return cls()
        doc = yaml.safe_load(Path(path).read_text()) or {}
        return cls({k: float(v) for k, v in (doc.get("terms") or {}).items()})

    def corrections(self, a, e):
        """(dA, dE) in radians at mechanical angles (A, E)."""
        c = {k: self.coefficients.get(k, 0.0) * ARCSEC for k in TERMS}
        a = np.asarray(a, dtype=float)
        e = np.asarray(e, dtype=float)
        cos_e = np.cos(e)
        cos_e = np.where(np.abs(cos_e) < _MIN_COS, np.copysign(_MIN_COS, cos_e + 0.0), cos_e)
        tan_e = np.sin(e) / cos_e
        sa, ca = np.sin(a), np.cos(a)
        da = c["IA"] + c["CA"] / cos_e + c["NPAE"] * tan_e - c["AN"] * sa * tan_e - c["AW"] * ca * tan_e
        de = c["IE"] - c["AN"] * ca + c["AW"] * sa + c["TF"] * np.cos(e) + np.where(e > np.pi / 2, c["BE"], 0.0)
        return da, de

    def apply(self, a, e):
        """Ideal mechanical angles -> commanded axis angles."""
        da, de = self.corrections(a, e)
        return np.asarray(a) + da, np.asarray(e) + de
