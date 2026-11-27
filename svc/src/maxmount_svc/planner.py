# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass finding and tracking plans for the over-the-top alt-az mount.

A plan maps time to mechanical axis angles for one of three modes:

    normal        exact tracking on the normal branch   (E = el)
    flipped       exact tracking on the flipped branch  (A = az + 180, E = 180 - el)
    over_the_top  azimuth held on the great circle through the zenith that best fits the pass;
                  elevation sweeps through the zenith. Pointing error = distance from that
                  circle (largest near culmination); used when exact tracking would exceed
                  the azimuth rate or acceleration limits.

Normal and flipped have identical azimuth rates (kinematics module); they differ only in which
part of the cable wrap and elevation range they use. Plans are checked against the axis limits
(with a rate margin), the horizon mask and the Sun.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import astro, kinematics
from .config import AxisLimits, Site, Tracking
from .pointing import PointingModel
from .targets import Target

NS = 1_000_000_000


@dataclass
class Pass:
    rise_ns: int
    culmination_ns: int
    set_ns: int
    max_el: float  # observed, rad
    culmination_az: float

    @property
    def duration_s(self) -> float:
        return (self.set_ns - self.rise_ns) / NS


@dataclass
class Plan:
    mode: str
    t_ns: np.ndarray  # sample times
    a: np.ndarray  # commanded mechanical azimuth (unwrapped), rad
    e: np.ndarray  # commanded mechanical elevation, rad
    max_error: float  # largest pointing error vs. the target, rad (0 for exact modes)
    peak_vel: tuple[float, float]
    peak_acc: tuple[float, float]
    problems: list[str] = field(default_factory=list)
    # How to evaluate the plan at arbitrary times (used to generate trajectory points).
    _target: Target | None = field(default=None, repr=False)
    _model: PointingModel | None = field(default=None, repr=False)
    _a0: float = 0.0  # over-the-top azimuth

    @property
    def feasible(self) -> bool:
        return not self.problems

    @property
    def start_ns(self) -> int:
        return int(self.t_ns[0])

    @property
    def end_ns(self) -> int:
        return int(self.t_ns[-1])

    def mech(self, t_ns):
        """Commanded axis angles at arbitrary times within (or near) the plan."""
        return self.mech_with(self._target, t_ns)

    def mech_with(self, target: Target, t_ns):
        """As mech, for another version of the target (e.g. with different TLE corrections)."""
        t = np.atleast_1d(np.asarray(t_ns, dtype=np.int64))
        a, e, _ = _mech_raw(self.mode, target, t, self._a0)
        a, e = self._model.apply(a, e) if self._model else (a, e)
        # Put each azimuth on the same 2*pi branch as the planned (unwrapped) path.
        ref = np.interp(t.astype(float), self.t_ns.astype(float), self.a)
        a = a + np.round((ref - a) / (2 * np.pi)) * 2 * np.pi
        return a, e


def _mech_raw(mode: str, target: Target, t_ns, a0: float):
    """Ideal mechanical angles and pointing error for a mode."""
    az, el = target.observed(t_ns)
    if mode in ("normal", "flipped"):
        a, e = kinematics.to_mech(az, el, mode == "flipped")
        return a, e, np.zeros_like(a)
    v = astro.azel_to_enu(az, el)
    h = np.array([np.sin(a0), np.cos(a0), 0.0])
    n = np.array([np.cos(a0), -np.sin(a0), 0.0])
    e = np.arctan2(v[..., 2], v @ h)  # 0 at the a0 horizon, pi/2 at zenith, pi opposite
    err = np.arcsin(np.clip(np.abs(v @ n), 0, 1))
    return np.full_like(e, a0), e, err


def _best_great_circle(az, el) -> float:
    """Azimuth of the vertical great circle that minimises the largest distance to the path."""
    v = astro.azel_to_enu(az, el)

    def worst(a0):
        n = np.stack([np.cos(a0), -np.sin(a0), np.zeros_like(a0)], axis=-1)
        return np.max(np.abs(v @ n.T), axis=0)

    grid = np.radians(np.arange(0.0, 180.0, 0.25))
    a0 = grid[np.argmin(worst(grid))]
    fine = a0 + np.radians(np.arange(-0.25, 0.25, 0.005))
    a0 = fine[np.argmin(worst(fine))]
    # Orient it so the pass starts on the a0 side (elevation sweeps 0 -> 180).
    start = v[0]
    if start @ np.array([np.sin(a0), np.cos(a0), 0.0]) < 0:
        a0 += np.pi
    return float(np.mod(a0, 2 * np.pi))


def _derivatives(t_ns, x):
    t = (np.asarray(t_ns) - t_ns[0]) / NS
    v = np.gradient(x, t)
    return v, np.gradient(v, t)


def _check(plan: Plan, az: AxisLimits, el: AxisLimits, tracking: Tracking):
    m = tracking.rate_margin
    va, aa = _derivatives(plan.t_ns, plan.a)
    ve, ae = _derivatives(plan.t_ns, plan.e)
    plan.peak_vel = (float(np.max(np.abs(va))), float(np.max(np.abs(ve))))
    plan.peak_acc = (float(np.max(np.abs(aa))), float(np.max(np.abs(ae))))
    deg = np.degrees
    if plan.peak_vel[0] > m * az.max_vel:
        plan.problems.append(f"az rate {deg(plan.peak_vel[0]):.1f} deg/s > {deg(m * az.max_vel):.1f}")
    if plan.peak_vel[1] > m * el.max_vel:
        plan.problems.append(f"el rate {deg(plan.peak_vel[1]):.1f} deg/s > {deg(m * el.max_vel):.1f}")
    if plan.peak_acc[0] > m * az.max_acc:
        plan.problems.append(f"az acceleration {deg(plan.peak_acc[0]):.1f} deg/s^2 > {deg(m * az.max_acc):.1f}")
    if plan.peak_acc[1] > m * el.max_acc:
        plan.problems.append(f"el acceleration {deg(plan.peak_acc[1]):.1f} deg/s^2 > {deg(m * el.max_acc):.1f}")
    if plan.e.min() < el.min_rad or plan.e.max() > el.max_rad:
        plan.problems.append(
            f"elevation axis {deg(plan.e.min()):.1f}..{deg(plan.e.max()):.1f} deg outside "
            f"{deg(el.min_rad):.0f}..{deg(el.max_rad):.0f}"
        )
    if plan.mode == "over_the_top" and plan.max_error > np.radians(tracking.over_the_top_tolerance_deg):
        plan.problems.append(
            f"over-the-top error {deg(plan.max_error):.2f} deg > tolerance {tracking.over_the_top_tolerance_deg:.2f}"
        )


def plan_mode(
    mode: str,
    target: Target,
    t0_ns: int,
    t1_ns: int,
    az: AxisLimits,
    el: AxisLimits,
    tracking: Tracking,
    site: Site | None = None,
    model: PointingModel | None = None,
    near_az: float | None = None,
    step_s: float = 0.1,
) -> Plan:
    t = np.arange(t0_ns, t1_ns + 1, int(step_s * NS), dtype=np.int64)
    sky_az, sky_el = target.observed(t)
    a0 = _best_great_circle(sky_az, sky_el) if mode == "over_the_top" else 0.0
    a, e, err = _mech_raw(mode, target, t, a0)
    if model:
        a, e = model.apply(a, e)
    plan = Plan(mode, t, a, e, float(err.max()), (0.0, 0.0), (0.0, 0.0), _target=target, _model=model, _a0=a0)

    unwrapped = kinematics.unwrap_into(a, az.min_rad, az.max_rad, near_az)
    if unwrapped is None:
        plan.problems.append("azimuth path does not fit the cable wrap")
    else:
        plan.a = unwrapped
    if sky_el.min() < np.radians(tracking.horizon_min_el_deg) - 1e-6:
        plan.problems.append(f"target below the {tracking.horizon_min_el_deg:.0f} deg horizon mask")
    if site is not None:
        ts = t[:: max(1, int(10 / step_s))]  # every 10 s
        sun_az, sun_el = astro.sun_observed(ts, site)
        taz, tel = target.observed(ts)
        sep = astro.separation(taz, tel, sun_az, sun_el)
        if np.min(sep) < np.radians(tracking.sun_min_sep_deg):
            plan.problems.append(f"passes {np.degrees(np.min(sep)):.0f} deg from the Sun")
    _check(plan, az, el, tracking)
    return plan


def choose_plan(plans: list[Plan]) -> Plan | None:
    """Exact modes first (normal before flipped), then over-the-top."""
    order = {"normal": 0, "flipped": 1, "over_the_top": 2}
    feasible = sorted((p for p in plans if p.feasible), key=lambda p: order[p.mode])
    return feasible[0] if feasible else None


def plan_all(target, t0_ns, t1_ns, az, el, tracking, site=None, model=None, near_az=None, modes=None):
    modes = modes or ("normal", "flipped", "over_the_top")
    plans = [plan_mode(m, target, t0_ns, t1_ns, az, el, tracking, site, model, near_az) for m in modes]
    return choose_plan(plans), plans


def find_passes(target: Target, t0_ns: int, t1_ns: int, min_el: float, step_s: float = 10.0) -> list[Pass]:
    """Intervals with the target above min_el (observed elevation), refined to ~10 ms."""
    t = np.arange(t0_ns, t1_ns + 1, int(step_s * NS), dtype=np.int64)
    _, el = target.observed(t)
    above = el >= min_el

    def crossing(lo, hi, rising):
        for _ in range(40):
            if hi - lo < 10_000_000:
                break
            mid = (lo + hi) // 2
            up = target.observed(np.int64(mid))[1] >= min_el
            if up == rising:
                hi = mid
            else:
                lo = mid
        return int(hi if rising else lo)

    passes = []
    i = 0
    while i < len(t):
        if not above[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(t) and above[j + 1]:
            j += 1
        rise = int(t[0]) if i == 0 else crossing(int(t[i - 1]), int(t[i]), True)
        set_ = int(t[-1]) if j == len(t) - 1 else crossing(int(t[j]), int(t[j + 1]), False)
        ts = np.linspace(rise, set_, 400).astype(np.int64)
        az_s, el_s = target.observed(ts)
        k = int(np.argmax(el_s))
        passes.append(Pass(rise, int(ts[k]), set_, float(el_s[k]), float(az_s[k])))
        i = j + 1
    return passes
