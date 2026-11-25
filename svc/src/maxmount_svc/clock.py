# SPDX-License-Identifier: GPL-3.0-or-later
"""System clock quality from chrony.

A LEO target near the zenith moves about 4 arcsec per millisecond, so satellite passes are
refused when the clock's error bound exceeds time.max_clock_error_us in the system file.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class ClockStatus:
    source: str
    error_bound_us: float  # |offset| + root dispersion + root delay / 2
    leap_status: str


def chrony_status(timeout: float = 2.0) -> ClockStatus | None:
    """None if chrony is not available."""
    try:
        out = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    f = out.stdout.strip().split(",")
    if len(f) < 14:
        return None
    # chronyc -c tracking: refid, name, stratum, ref time, system offset (s), last offset,
    # rms offset, frequency, residual freq, skew, root delay (s), root dispersion (s),
    # update interval, leap status
    offset, root_delay, root_disp = abs(float(f[4])), float(f[10]), float(f[11])
    return ClockStatus(source=f[1], error_bound_us=(offset + root_disp + root_delay / 2) * 1e6, leap_status=f[13])
