# SPDX-License-Identifier: GPL-3.0-or-later
"""CelesTrak SOCRATES Plus conjunctions, screened for what the observatory can see.

SOCRATES screens active satellites against the whole catalogue three times a day, 7 days
ahead, for close approaches within 5 km (https://celestrak.org/SOCRATES/). The full result
set is one CSV (sort-minRange.csv, ~20 MB, ~200 000 rows; format at
https://celestrak.org/SOCRATES/socrates-format.php):

    NORAD_CAT_ID_1, OBJECT_NAME_1, DSE_1, NORAD_CAT_ID_2, OBJECT_NAME_2, DSE_2,
    TCA, TCA_RANGE (km), TCA_RELATIVE_SPEED (km/s), MAX_PROB, DILUTION (km)

It is downloaded at most once per SOCRATES run (8 h) and cached. At the time of closest
approach (TCA) the two objects are within a few km of each other, so one object's position
says whether the event is above the horizon, sunlit and in a dark sky: screening uses
whichever object has an element set in the CelesTrak 'active' group (one of the two is an
active payload). The exact element sets SOCRATES used for a pair come from
data.php?CATNR=a,b (OMM CSV), fetched only for a conjunction the user selects.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import erfa
import numpy as np
from sgp4 import exporter, omm
from sgp4.api import Satrec

from . import astro

log = logging.getLogger(__name__)

CSV_URL = "https://celestrak.org/SOCRATES/sort-minRange.csv"
DATA_URL = "https://celestrak.org/SOCRATES/data.php?CATNR={a},{b}"
MAX_AGE_S = 8 * 3600
NS = 1_000_000_000
RE_KM = 6378.137


@dataclass
class Conjunction:
    cat1: str
    name1: str
    dse1: float
    cat2: str
    name2: str
    dse2: float
    tca_ns: int
    range_km: float
    speed_km_s: float
    max_prob: float
    dilution_km: float


def _tca_ns(s: str) -> int:
    """'2026-12-01 08:54:06.014' (UTC) -> ns since the Unix epoch."""
    return int(np.datetime64(s.strip().replace(" ", "T"), "ns").astype(np.int64))


def parse_csv(text: str) -> list[Conjunction]:
    out = []
    rows = csv.reader(io.StringIO(text))
    header = next(rows, None)
    if not header or header[0].strip() != "NORAD_CAT_ID_1":
        raise ValueError("not a SOCRATES CSV")
    for r in rows:
        if len(r) < 11:
            continue
        try:
            out.append(Conjunction(r[0].strip(), r[1].strip(), float(r[2]), r[3].strip(), r[4].strip(), float(r[5]),
                                   _tca_ns(r[6]), float(r[7]), float(r[8]), float(r[9]), float(r[10])))
        except ValueError:
            continue
    return out


def omm_to_tles(text: str) -> dict[str, tuple[str, str, str]]:
    """data.php OMM CSV -> {catalog number: (name, line1, line2)}. Objects whose catalog number
    does not fit a TLE (> 339999) are left out."""
    out = {}
    for fields in omm.parse_csv(io.StringIO(text)):
        try:
            sat = Satrec()
            omm.initialize(sat, fields)
            l1, l2 = exporter.export_tle(sat)
        except Exception:  # noqa: BLE001 - one bad record must not lose the other
            continue
        out[str(int(fields["NORAD_CAT_ID"]))] = (fields.get("OBJECT_NAME", "").strip(), l1, l2)
    return out


class Socrates:
    def __init__(self, cache_dir: Path | None = None):
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.path = (cache_dir or base / "maxmount") / "socrates-minrange.csv"
        self._conj: list[Conjunction] | None = None
        self._mtime = 0.0
        self._pairs: dict[tuple[str, str], dict] = {}
        self.note = "not loaded"

    @classmethod
    def from_text(cls, text: str) -> Socrates:
        s = cls()
        s._conj, s._mtime, s.note = parse_csv(text), time.time(), "test data"
        s.path = None
        return s

    def load(self) -> list[Conjunction]:
        if self.path is None:
            return self._conj
        stale = not self.path.exists() or time.time() - self.path.stat().st_mtime > MAX_AGE_S
        if stale:
            try:
                req = urllib.request.Request(CSV_URL, headers={"User-Agent": "maxmount"})
                tmp = self.path.with_suffix(".part")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:  # noqa: S310 - fixed URL
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                with open(tmp) as f:
                    if not f.readline().startswith("NORAD_CAT_ID_1"):
                        raise OSError("unexpected contents")
                tmp.replace(self.path)
            except OSError as e:
                log.warning("SOCRATES download failed: %s", e)
                if not self.path.exists():
                    raise RuntimeError(f"cannot fetch SOCRATES: {e}") from None
        mtime = self.path.stat().st_mtime
        if self._conj is None or mtime != self._mtime:
            self._conj, self._mtime = parse_csv(self.path.read_text()), mtime
        age_h = (time.time() - mtime) / 3600
        self.note = f"SOCRATES {age_h:.1f} h old, {len(self._conj)} conjunctions"
        return self._conj

    def pair(self, a: str, b: str) -> dict[str, tuple[str, str, str]]:
        """The element sets SOCRATES used for this pair (cached for the session)."""
        key = (a, b)
        if key not in self._pairs:
            req = urllib.request.Request(DATA_URL.format(a=a, b=b), headers={"User-Agent": "maxmount"})
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed host
                self._pairs[key] = omm_to_tles(r.read().decode("utf-8", "replace"))
        return self._pairs[key]


def screen(conj: list[Conjunction], tles: dict[str, tuple[str, str, str]], site, t0_ns: int, t1_ns: int,
           max_range_km: float, min_el_deg: float) -> list[dict]:
    """Conjunctions between t0 and t1, within max_range_km, with the event above min_el_deg at
    the site. Each result has the conjunction fields plus: primary (catalog number used for
    the position), az_deg / el_deg / obs_range_km of the event, sunlit (the event is in
    sunlight), sun_el_deg (at the site), phase_rad
    (Sun-satellite-observer angle)."""
    cand = [c for c in conj if t0_ns <= c.tca_ns <= t1_ns and c.range_km <= max_range_km
            and (c.cat1 in tles or c.cat2 in tles)]
    if not cand:
        return []
    sats: dict[str, Satrec] = {}
    r_teme = np.full((len(cand), 3), np.nan)
    prim = []
    for i, c in enumerate(cand):
        p = c.cat1 if c.cat1 in tles else c.cat2
        prim.append(p)
        if p not in sats:
            _n, l1, l2 = tles[p]
            sats[p] = Satrec.twoline2rv(l1, l2)
        jd, fr = divmod(c.tca_ns / NS / 86400 + astro.UNIX_EPOCH_JD, 1.0)
        err, r, _v = sats[p].sgp4(jd, fr)
        if err == 0:
            r_teme[i] = r
    t = np.array([c.tca_ns for c in cand], dtype=np.int64)
    ok = np.isfinite(r_teme[:, 0])
    pef = astro.teme_to_pef(np.where(ok[:, None], r_teme, 0.0), t, site.dut1_s)
    enu = (pef - astro.site_itrs_km(site)) @ astro.enu_basis(site).T
    rng = np.linalg.norm(enu, axis=-1)
    az, el = astro.enu_to_azel(enu)
    keep = ok & (np.degrees(el) >= min_el_deg)
    if not keep.any():
        return []
    idx = np.nonzero(keep)[0]
    # Earth's shadow (cylinder) at TCA, and the Sun's elevation at the site.
    t1, t2 = astro.tt_jd(t[idx])
    pvh, _ = erfa.epv00(t1, t2)
    sun = -np.asarray(pvh["p"])
    sun /= np.linalg.norm(sun, axis=-1, keepdims=True)
    r = r_teme[idx]
    along = np.sum(r * sun, axis=-1)
    lit = (along > 0) | (np.linalg.norm(r - along[:, None] * sun, axis=-1) > RE_KM)
    saz, sel = astro.sun_observed(t[idx], site)
    sun_enu = astro.azel_to_enu(saz, sel)
    out = []
    for k, i in enumerate(idx):
        c = cand[i]
        out.append({
            "cat1": c.cat1, "name1": c.name1, "dse1": c.dse1, "cat2": c.cat2, "name2": c.name2, "dse2": c.dse2,
            "tca_ns": str(c.tca_ns), "range_km": c.range_km, "speed_km_s": c.speed_km_s, "max_prob": c.max_prob,
            "dilution_km": c.dilution_km, "primary": prim[i],
            "az_deg": round(float(np.degrees(az[i])), 2), "el_deg": round(float(np.degrees(el[i])), 2),
            "obs_range_km": round(float(rng[i]), 1), "sunlit": bool(lit[k]), "sun_el_deg": round(float(np.degrees(sel[k])), 1),
            # Sun-satellite-observer angle, for the predicted magnitude.
            "phase_rad": float(np.arccos(np.clip(-(enu[i] / rng[i]) @ sun_enu[k], -1, 1))),
        })
    return out
