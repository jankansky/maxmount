# SPDX-License-Identifier: GPL-3.0-or-later
"""Observatory position from gpsd (https://gpsd.io/gpsd_json.html).

Averages several 3D fixes. Height is taken above the WGS84 ellipsoid (`altHAE`), which is what
ERFA's site model expects (not `altMSL`). The result is cached so a portable setup knows where
it is even when the receiver has no fix (indoors, cold start); a stale cache is reported.

Position errors matter little here: 5 m moves a 400 km LEO satellite by ~2.6 arcsec and stars
not at all; the pointing model absorbs what is left.
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Fix:
    latitude_deg: float
    longitude_deg: float
    height_m: float  # above the WGS84 ellipsoid
    n_fixes: int
    eph_m: float  # gpsd's horizontal error estimate (95%), mean over the fixes
    time_unix: float  # when the average was taken
    source: str = "gpsd"


class GnssError(RuntimeError):
    pass


def cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "maxmount" / "site-gnss.json"


def _height(tpv: dict) -> float | None:
    if "altHAE" in tpv:
        return float(tpv["altHAE"])
    if "alt" in tpv and "geoidSep" in tpv:  # older gpsd: alt is MSL
        return float(tpv["alt"]) + float(tpv["geoidSep"])
    return None


def read_fix(host: str = "localhost", port: int = 2947, n: int = 10, timeout: float = 20.0) -> Fix:
    """Averages n 3D fixes from gpsd. Raises GnssError if none arrive within timeout."""
    deadline = time.monotonic() + timeout
    lats, lons, hs, ephs = [], [], [], []
    try:
        with socket.create_connection((host, port), timeout=min(5.0, timeout)) as s:
            s.sendall(b'?WATCH={"enable":true,"json":true};\n')
            buf = b""
            while len(lats) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                s.settimeout(left)
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") != "TPV" or msg.get("mode", 0) < 3:
                        continue
                    h = _height(msg)
                    if h is None or "lat" not in msg or "lon" not in msg:
                        continue
                    lats.append(float(msg["lat"]))
                    lons.append(float(msg["lon"]))
                    hs.append(h)
                    ephs.append(float(msg.get("eph", math.nan)))
    except OSError as e:
        raise GnssError(f"cannot reach gpsd at {host}:{port}: {e}") from None
    if not lats:
        raise GnssError(f"no 3D fix from gpsd at {host}:{port} within {timeout:.0f} s")
    # Longitudes near +-180 would need circular averaging; fixes this close together do not.
    return Fix(
        latitude_deg=sum(lats) / len(lats),
        longitude_deg=sum(lons) / len(lons),
        height_m=sum(hs) / len(hs),
        n_fixes=len(lats),
        eph_m=sum(ephs) / len(ephs),
        time_unix=time.time(),
    )


def save(fix: Fix, path: Path | None = None) -> None:
    path = path or cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(fix), indent=2))
    tmp.replace(path)


def load_cached(path: Path | None = None) -> Fix | None:
    path = path or cache_path()
    try:
        return Fix(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def site_fix(host: str = "localhost", port: int = 2947, timeout: float = 20.0, max_cache_age_days: float = 30.0) -> tuple[Fix, str]:
    """A live fix (cached for next time), else the cached one. Returns (fix, description)."""
    try:
        fix = read_fix(host, port, timeout=timeout)
        save(fix)
        return fix, f"gpsd, {fix.n_fixes} fixes, eph {fix.eph_m:.1f} m"
    except GnssError as e:
        cached = load_cached()
        if cached is None:
            raise GnssError(f"{e}, and no cached position: move the antenna to see the sky") from None
        age = (time.time() - cached.time_unix) / 86400
        if age > max_cache_age_days:
            raise GnssError(f"{e}; the cached position is {age:.0f} days old (move the antenna to see the sky)") from None
        log.warning("%s; using the cached GNSS position from %.1f days ago", e, age)
        return cached, f"cached GNSS fix, {age:.1f} days old"
