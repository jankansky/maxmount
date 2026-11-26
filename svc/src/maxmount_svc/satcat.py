# SPDX-License-Identifier: GPL-3.0-or-later
"""Satellite brightness and size: Mike McCants' quicksat.mag catalogue, plus local overrides.

quicksat.mag (https://www.mmccants.org/programs/qsmag.zip) gives, per catalog number, an
intrinsic magnitude, dimensions and radar cross-section (RCS). Its intrinsic magnitude is the
"Quicksat" standard: 1000 km range, full phase, brightest likely orientation. (The Molczan /
"mcnames" standard, at 90 deg phase and average orientation, is about 1.4 magnitudes fainter.)
See https://www.mmccants.org/tles/intrmagdef.html.

Where the catalogue has an RCS but no magnitude or size, both are estimated from the RCS using
fits to the catalogue's own entries (marked as estimates):
    std_mag ~ 6.3 - 2.5 log10(RCS)   (1 sigma ~0.8 mag)
    length  ~ 2.0 sqrt(RCS) m        (1 sigma ~ factor 1.7)

config/satellites.yaml adds or replaces values (keyed by catalog number).

Predicted magnitude, consistent with the file's definition (illuminated-fraction phase law,
which gives the ~0.7 mag between full and 90 deg phase that the definitions page describes):
    m = std_mag + 5 log10(range / 1000 km) - 2.5 log10((1 + cos phase) / 2) + k (airmass - 1)
with k = 0.2 mag per airmass (V band). Specular flares (solar panels, Starlink) and tumbling
are not modelled.
"""
from __future__ import annotations

import io
import logging
import math
import os
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

QSMAG_URL = "https://www.mmccants.org/programs/qsmag.zip"
MAX_AGE_S = 30 * 86400
RCS_MAG0 = 6.3        # std_mag ~ RCS_MAG0 - 2.5 log10(RCS m^2)
RCS_LENGTH = 2.0      # length ~ RCS_LENGTH sqrt(RCS) m
EXTINCTION = 0.2      # mag per airmass
REPO = Path(__file__).resolve().parents[3]
OVERRIDES = REPO / "config" / "satellites.yaml"


@dataclass
class SatInfo:
    std_mag: float | None = None
    mag_estimated: bool = False     # from the RCS
    size_m: tuple[float, ...] | None = None
    size_estimated: bool = False
    rcs_m2: float | None = None
    source: str = ""
    comment: str = ""

    @property
    def max_size_m(self) -> float | None:
        return max(self.size_m) if self.size_m else None


def norad_key(s: str) -> str:
    """Catalog number as in a TLE or quicksat.mag (5 characters, Alpha-5 allowed) -> key."""
    s = s.strip()
    return s.lstrip("0") or "0"


def _num(field: str) -> float | None:
    field = field.strip()
    if not field:
        return None
    try:
        return float(field)
    except ValueError:
        return None


def parse_quicksat_mag(text: str) -> dict[str, SatInfo]:
    """Fixed columns: number 0-4, flag 6, designation 8-17, name 18-32, magnitude 33-36,
    sizes 37-41 / 42-45 / 46-49 (m; size 3 = 0: a cylinder of diameter size 2), RCS 50-54,
    comment 55-."""
    out: dict[str, SatInfo] = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if len(line) < 34 or not line[:5].strip() or "Desig..." in line:
            continue
        mag = _num(line[33:37])
        sizes = tuple(v for v in (_num(line[37:42]), _num(line[42:46]), _num(line[46:50])) if v)
        rcs = _num(line[50:55])
        comment = line[55:].strip()
        if mag is None and not sizes and not rcs:
            continue
        out[norad_key(line[:5])] = SatInfo(std_mag=mag, size_m=sizes or None, rcs_m2=rcs if rcs and rcs > 0 else None,
                                           source="quicksat.mag", comment=comment)
    return out


def _fill_from_rcs(info: SatInfo) -> SatInfo:
    if info.rcs_m2:
        if info.std_mag is None:
            info.std_mag, info.mag_estimated = round(RCS_MAG0 - 2.5 * math.log10(info.rcs_m2), 1), True
        if not info.size_m:
            info.size_m, info.size_estimated = (round(RCS_LENGTH * math.sqrt(info.rcs_m2), 1),), True
    return info


class SatCatalog:
    """quicksat.mag cached on disk (refreshed monthly), with config/satellites.yaml on top."""

    def __init__(self, cache_dir: Path | None = None, overrides: Path | None = OVERRIDES):
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.path = (cache_dir or base / "maxmount") / "quicksat.mag"
        self.overrides_path = overrides
        self._data: dict[str, SatInfo] | None = None
        self.note = "not loaded"

    @classmethod
    def from_text(cls, text: str, overrides: Path | None = None) -> SatCatalog:
        cat = cls(overrides=overrides)
        cat._data = cls._merge(parse_quicksat_mag(text), overrides)
        cat.note = "test data"
        return cat

    @staticmethod
    def _merge(data: dict[str, SatInfo], overrides: Path | None) -> dict[str, SatInfo]:
        if overrides and overrides.exists():
            doc = yaml.safe_load(overrides.read_text()) or {}
            for key, v in doc.items():
                k = norad_key(str(key))
                info = data.get(k) or SatInfo()
                if "std_mag" in v:
                    info.std_mag, info.mag_estimated = float(v["std_mag"]), False
                if "size_m" in v:
                    info.size_m, info.size_estimated = tuple(float(x) for x in v["size_m"] if float(x) > 0), False
                info.source = "satellites.yaml" if not info.source else info.source + " + satellites.yaml"
                info.comment = v.get("note", info.comment)
                data[k] = info
        for info in data.values():
            _fill_from_rcs(info)
        return data

    def load(self) -> str:
        if self._data is not None:
            return self.note
        fresh = self.path.exists() and time.time() - self.path.stat().st_mtime < MAX_AGE_S
        if not fresh:
            try:
                req = urllib.request.Request(QSMAG_URL, headers={"User-Agent": "maxmount"})
                with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https URL
                    blob = r.read()
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    name = next(n for n in z.namelist() if n.lower().endswith(".mag"))
                    text = z.read(name).decode("latin-1")
                if "Desig" not in text[:200]:
                    raise OSError("unexpected contents")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(text)
            except (OSError, StopIteration, zipfile.BadZipFile) as e:
                log.warning("quicksat.mag download failed: %s", e)
                if not self.path.exists():
                    self._data = self._merge({}, self.overrides_path)
                    self.note = f"no magnitude catalogue ({e})"
                    return self.note
        self._data = self._merge(parse_quicksat_mag(self.path.read_text()), self.overrides_path)
        age_d = (time.time() - self.path.stat().st_mtime) / 86400
        self.note = f"quicksat.mag {age_d:.0f} d old"
        return self.note

    def get(self, catalog_number: str) -> SatInfo | None:
        self.load()
        return self._data.get(norad_key(catalog_number))


def predicted_mag(std_mag: float, range_km, phase_rad, el_rad):
    """Visual magnitude (works on numpy arrays)."""
    import numpy as np

    fracil = np.clip((1 + np.cos(phase_rad)) / 2, 1e-4, 1)
    airmass = 1 / np.sin(np.clip(el_rad, math.radians(3), None))
    return std_mag + 5 * np.log10(np.asarray(range_km) / 1000) - 2.5 * np.log10(fracil) + EXTINCTION * (airmass - 1)
