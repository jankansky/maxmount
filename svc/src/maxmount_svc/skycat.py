# SPDX-License-Identifier: GPL-3.0-or-later
"""The offline sky catalogue (gui/vendor/sky, built by tools/build_skydata.py): stars to mag 8
(HYG), deep-sky objects (OpenNGC) and constellations. Shared by the sky chart's search, the
plate solver and the widefield camera simulator. Positions ICRS J2000."""
from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SKY_DIR = Path(__file__).resolve().parents[3] / "gui" / "vendor" / "sky"
DEG = math.pi / 180
MAS = DEG / 3600 / 1000


@dataclass
class Stars:
    ra: np.ndarray     # rad, J2000 at epoch 2000.0
    dec: np.ndarray
    mag: np.ndarray
    bv: np.ndarray
    pmra: np.ndarray   # rad/yr on the sky (RA one scaled by cos dec)
    pmdec: np.ndarray
    hip: np.ndarray
    names: dict        # index -> [proper name, designation]

    def at_epoch(self, year: float) -> tuple[np.ndarray, np.ndarray]:
        """(ra, dec) in rad with proper motion applied to the given Julian year."""
        dt = year - 2000.0
        dec = self.dec + self.pmdec * dt
        ra = self.ra + self.pmra * dt / np.maximum(np.cos(self.dec), 1e-6)
        return np.mod(ra, 2 * math.pi), dec

    def unit(self, year: float = 2000.0) -> np.ndarray:
        ra, dec = self.at_epoch(year)
        return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)

    def cone(self, ra: float, dec: float, radius: float, mag_max: float = 99.0, year: float = 2000.0) -> np.ndarray:
        """Indices of stars within radius (rad) of (ra, dec), brightest first."""
        c = np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])
        cos_d = self.unit(year) @ c
        return np.flatnonzero((cos_d >= math.cos(radius)) & (self.mag <= mag_max))


@functools.lru_cache(maxsize=None)
def stars(sky_dir: Path = SKY_DIR) -> Stars:
    d = json.loads((sky_dir / "stars.json").read_text())
    f = lambda k: np.asarray(d[k], dtype=float)  # noqa: E731
    return Stars(ra=f("ra") * DEG, dec=f("dec") * DEG, mag=f("mag"), bv=f("bv"), pmra=f("pmra") * MAS,
                 pmdec=f("pmdec") * MAS, hip=np.asarray(d["hip"], dtype=np.int64),
                 names={int(k): v for k, v in d["names"].items()})


@functools.lru_cache(maxsize=None)
def dso(sky_dir: Path = SKY_DIR) -> list[dict]:
    return json.loads((sky_dir / "dso.json").read_text())["objects"]


def _norm(s: str) -> str:
    return "".join(s.lower().split())


def find(query: str, limit: int = 20) -> list[dict]:
    """Stars and deep-sky objects whose name, designation or catalogue id matches the query
    (case and spaces ignored; exact matches first). Each: {kind, name, ra_deg, dec_deg, mag, ...}."""
    q = _norm(query)
    if not q:
        return []
    exact, prefix, part = [], [], []

    def add(item, keys):
        ks = [_norm(k) for k in keys if k]
        if q in ks:
            exact.append(item)
        elif any(k.startswith(q) for k in ks):
            prefix.append(item)
        elif len(q) >= 3 and any(q in k for k in ks):
            part.append(item)

    for o in dso():
        keys = [o["id"], o["alt"], o["common"], o["id"].replace("M ", "Messier ")]
        add({"kind": "dso", "name": o["id"], **{k: o[k] for k in ("alt", "common", "type", "mag", "maj", "min", "pa", "con")},
             "ra_deg": o["ra"], "dec_deg": o["dec"]}, keys)
    s = stars()
    for i, (proper, desig) in s.names.items():
        add({"kind": "star", "name": proper or desig, "alt": desig if proper else "", "mag": float(s.mag[i]),
             "hip": int(s.hip[i]), "ra_deg": math.degrees(s.ra[i]), "dec_deg": math.degrees(s.dec[i]),
             "pm_ra_mas_yr": float(s.pmra[i] / MAS), "pm_dec_mas_yr": float(s.pmdec[i] / MAS)},
            [proper, desig, f"HIP{int(s.hip[i])}" if s.hip[i] else ""])
    return (exact + prefix + part)[:limit]
