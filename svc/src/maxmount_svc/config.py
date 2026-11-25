# SPDX-License-Identifier: GPL-3.0-or-later
"""mount-svc settings from the shared system file (config/systems/<name>/system.yaml).

mount-svc reads the sections it needs: site, tracking, axes (names and limits), zenoh and
time. mount-rt ignores site and tracking.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEG = math.pi / 180.0


@dataclass
class Site:
    latitude_deg: float
    longitude_deg: float  # east positive
    height_m: float = 0.0
    pressure_hpa: float = 1013.25  # 0 disables refraction
    temperature_c: float = 10.0
    humidity: float = 0.5  # 0..1
    wavelength_um: float = 0.55
    dut1_s: float = 0.0  # UT1 - UTC; "auto" in the system file reads IERS Bulletin A
    dut1_source: str = "system file"


@dataclass
class AxisLimits:
    min_rad: float
    max_rad: float
    max_vel: float  # rad/s
    max_acc: float  # rad/s^2


@dataclass
class Tracking:
    horizon_min_el_deg: float = 10.0
    sun_min_sep_deg: float = 30.0
    slew_vel_deg_s: float = 5.0
    slew_acc_deg_s2: float = 5.0
    rate_margin: float = 0.9  # plans may use this fraction of the axis velocity/acceleration
    over_the_top_tolerance_deg: float = 1.0  # max pointing error accepted in over-the-top mode
    point_rate_hz: float = 20.0
    lookahead_s: float = 5.0


@dataclass
class ZenohSettings:
    site: str = "default"
    mode: str = "peer"
    connect: list[str] = field(default_factory=list)
    listen: list[str] = field(default_factory=list)
    multicast_scouting: bool = True


@dataclass
class SystemSettings:
    name: str
    backend: str
    site: Site | None
    tracking: Tracking
    az: AxisLimits
    el: AxisLimits
    zenoh: ZenohSettings
    max_clock_error_us: float = 10.0
    pointing_file: Path | None = None


class ConfigError(RuntimeError):
    pass


def _limits(axis: dict, name: str) -> AxisLimits:
    lim = axis.get("limits") or {}
    try:
        return AxisLimits(
            float(lim["min_deg"]) * DEG,
            float(lim["max_deg"]) * DEG,
            float(lim["max_vel_deg_s"]) * DEG,
            float(lim["max_acc_deg_s2"]) * DEG,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"axis '{name}': incomplete limits ({e})") from None


def load_system(path: str | Path) -> SystemSettings:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    axes = {a.get("name"): a for a in doc.get("axes") or []}
    for needed in ("az", "el"):
        if needed not in axes:
            raise ConfigError(f"{path}: mount-svc needs axes named 'az' and 'el'")

    site = None
    s = dict(doc.get("site") or {})
    if s.get("latitude_deg") is not None and s.get("longitude_deg") is not None:
        auto_dut1 = str(s.get("dut1_s", "")).strip().lower() == "auto"
        if auto_dut1:
            s.pop("dut1_s")
        site = Site(**{k: float(v) for k, v in s.items() if k in Site.__dataclass_fields__ and v is not None})
        if auto_dut1:
            import time

            from . import iers

            site.dut1_s, source = iers.dut1_at(time.time_ns())
            site.dut1_source = source

    t = doc.get("tracking") or {}
    tracking = Tracking(**{k: float(v) for k, v in t.items() if k in Tracking.__dataclass_fields__})

    z = doc.get("zenoh") or {}
    zenoh = ZenohSettings(
        site=str(z.get("site", "default")),
        mode=str(z.get("mode", "peer")),
        connect=list(z.get("connect") or []),
        listen=list(z.get("listen") or []),
        multicast_scouting=bool(z.get("multicast_scouting", True)),
    )

    pointing = doc.get("pointing")
    pointing_file = (path.parent / pointing).resolve() if pointing else None

    return SystemSettings(
        name=str(doc.get("system", path.parent.name)),
        backend=str((doc.get("fieldbus") or {}).get("backend", "sim")),
        site=site,
        tracking=tracking,
        az=_limits(axes["az"], "az"),
        el=_limits(axes["el"], "el"),
        zenoh=zenoh,
        max_clock_error_us=float((doc.get("time") or {}).get("max_clock_error_us", 10.0)),
        pointing_file=pointing_file,
    )
