# SPDX-License-Identifier: GPL-3.0-or-later
"""UT1 - UTC from IERS Bulletin A (finals.daily, IAU 2000).

Earth rotation enters satellite positions directly: 0.1 s of UT1 error moves a LEO satellite
~45 m, about 20 arcsec at 500 km range. UT1 - UTC drifts by ~1 ms/day, so a file refreshed
weekly is ample. The file is cached in $XDG_CACHE_HOME/maxmount (or ~/.cache/maxmount) so the
observatory keeps working offline with the last download.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

URL = "https://datacenter.iers.org/data/latestVersion/finals.daily.iau2000.txt"
MJD_UNIX_EPOCH = 40587.0
MAX_AGE_S = 7 * 86400


def cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "maxmount" / "finals.daily.iau2000.txt"


def parse(text: str) -> list[tuple[float, float]]:
    """(MJD, UT1-UTC seconds) rows. Bulletin A fixed columns: MJD 8-15, UT1-UTC 59-68."""
    rows = []
    for line in text.splitlines():
        if len(line) < 68:
            continue
        try:
            mjd = float(line[7:15])
            dut1 = line[58:68].strip()
            if dut1:
                rows.append((mjd, float(dut1)))
        except ValueError:
            continue
    return rows


def interpolate(rows: list[tuple[float, float]], mjd: float) -> float:
    if not rows:
        raise ValueError("no UT1-UTC rows")
    if mjd <= rows[0][0]:
        return rows[0][1]
    if mjd >= rows[-1][0]:
        return rows[-1][1]
    for (m0, d0), (m1, d1) in zip(rows, rows[1:]):
        if m0 <= mjd <= m1:
            if abs(d1 - d0) > 0.5:  # leap second between the two days: no interpolation
                return d0 if mjd < m1 else d1
            return d0 + (d1 - d0) * (mjd - m0) / (m1 - m0)
    return rows[-1][1]


def _download(path: Path, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as r:  # noqa: S310 - fixed https URL
            data = r.read()
    except OSError as e:
        log.warning("IERS download failed: %s", e)
        return False
    if not parse(data.decode("ascii", "replace")):
        log.warning("IERS download did not contain Bulletin A data")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


def dut1_at(t_ns: int, download: bool = True, timeout: float = 20.0) -> tuple[float, str]:
    """UT1 - UTC (s) at t, and a note on where it came from. Falls back to 0 with a warning."""
    path = cache_path()
    stale = not path.exists() or time.time() - path.stat().st_mtime > MAX_AGE_S
    if stale and download:
        _download(path, timeout)
    if not path.exists():
        log.warning("no IERS data: using UT1-UTC = 0 (satellite positions off by up to ~20 arcsec)")
        return 0.0, "none (0)"
    rows = parse(path.read_text(errors="replace"))
    mjd = MJD_UNIX_EPOCH + t_ns / 86_400e9
    age_days = (time.time() - path.stat().st_mtime) / 86400
    if age_days > 7:
        log.warning("IERS data is %.0f days old", age_days)
    if rows and not rows[0][0] <= mjd <= rows[-1][0]:
        log.warning("time is outside the IERS table (MJD %.0f-%.0f): using the nearest value", rows[0][0], rows[-1][0])
    return interpolate(rows, mjd), f"IERS Bulletin A ({age_days:.0f} d old)"
