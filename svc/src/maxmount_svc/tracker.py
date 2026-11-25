# SPDX-License-Identifier: GPL-3.0-or-later
"""Executes a plan on mount-rt: enable, slew to where the target will be, then stream the
tracking trajectory a few seconds ahead. mount-rt interpolates the points (cubic Hermite) and
enforces limits; if the stream stops, its trajectory runs out and the axes decelerate and hold.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

import numpy as np

from .config import Tracking
from .mount import MountClient, MountError
from .planner import NS, Plan
from .proto import mpb

log = logging.getLogger(__name__)
AXES = ("az", "el")


class MoveProfile:
    """Rest-to-rest trapezoidal move (same as mount-rt's MoveProfile)."""

    def __init__(self, start: float, end: float, max_vel: float, max_acc: float):
        self.p0 = start
        self.dir = 1.0 if end >= start else -1.0
        self.dist = abs(end - start)
        self.a = max_acc
        self.v = min(max_vel, math.sqrt(self.dist * max_acc))
        self.t_ramp = self.v / max_acc
        self.t_cruise = (self.dist - self.v * self.t_ramp) / self.v if self.v > 0 else 0.0

    @property
    def duration(self) -> float:
        return 2 * self.t_ramp + self.t_cruise

    def at(self, t: float) -> tuple[float, float]:
        t = min(max(t, 0.0), self.duration)
        if t < self.t_ramp:
            s, v = 0.5 * self.a * t * t, self.a * t
        elif t < self.t_ramp + self.t_cruise:
            s, v = 0.5 * self.v * self.t_ramp + self.v * (t - self.t_ramp), self.v
        else:
            td = self.duration - t
            s, v = self.dist - 0.5 * self.a * td * td, self.a * td
        return self.p0 + self.dir * s, self.dir * v


def plan_points(
    plan: Plan, t0_ns: int, t1_ns: int, step_ns: int, d_az: float = 0.0, d_el: float = 0.0
) -> dict[str, list[tuple[int, float, float]]]:
    """Time-tagged positions and velocities (central differences) from a plan, plus constant
    axis offsets (rad)."""
    t = np.arange(t0_ns, t1_ns + 1, step_ns, dtype=np.int64)
    if len(t) == 0:
        return {a: [] for a in AXES}
    if t[-1] < t1_ns:  # always end exactly at t1 (e.g. the end of the plan)
        t = np.append(t, np.int64(t1_ns))
    h = 5_000_000  # 5 ms
    a, e = plan.mech(t)
    a_p, e_p = plan.mech(t + h)
    a_m, e_m = plan.mech(t - h)
    va = (a_p - a_m) / (2 * h / NS)
    ve = (e_p - e_m) / (2 * h / NS)
    return {
        "az": list(zip(t.tolist(), (a + d_az).tolist(), va.tolist())),
        "el": list(zip(t.tolist(), (e + d_el).tolist(), ve.tolist())),
    }


@dataclass
class Slew:
    t0_ns: int
    t_join_ns: int
    profiles: dict[str, MoveProfile]

    def points(self, step_ns: int) -> dict[str, list[tuple[int, float, float]]]:
        out: dict[str, list[tuple[int, float, float]]] = {a: [] for a in AXES}
        t = self.t0_ns
        while True:
            for axis, prof in self.profiles.items():
                p, v = prof.at((t - self.t0_ns) / NS)
                out[axis].append((t, p, v))
            if t >= self.t_join_ns:
                break
            t = min(t + step_ns, self.t_join_ns)
        return out


def plan_slew(
    plan: Plan,
    start: dict[str, float],
    now_ns: int,
    tracking: Tracking,
    lead_s: float = 0.5,
    offsets: tuple[float, float] = (0.0, 0.0),
) -> Slew:
    """Slew from `start` to where the plan (plus axis offsets) will be when the slew ends,
    joining the plan at the later of its start and the slew's arrival."""
    vel = math.radians(tracking.slew_vel_deg_s)
    acc = math.radians(tracking.slew_acc_deg_s2)
    t0 = now_ns + int(lead_s * NS)

    def profiles_to(t_join):
        a, e = plan.mech(np.int64(t_join))
        a0, e0 = float(a[0]) + offsets[0], float(e[0]) + offsets[1]
        return {"az": MoveProfile(start["az"], a0, vel, acc), "el": MoveProfile(start["el"], e0, vel, acc)}

    t_join = max(plan.start_ns, t0)
    for _ in range(8):  # the target moves while we slew: iterate on the join time
        arrive = t0 + int(max(p.duration for p in profiles_to(t_join).values()) * NS) + 100_000_000  # 0.1 s settle
        if arrive <= t_join:
            break
        t_join = arrive
    return Slew(t0, t_join, profiles_to(t_join))


class TrackAborted(RuntimeError):
    """Tracking ended because of a fault, a trip or an error; the drives were disabled."""


class TrackStopped(RuntimeError):
    """Tracking was stopped on request. reason: "stop" (drives disabled), "hold" (holding
    position, enabled) or "retarget" (left as is for the next track to take over)."""

    def __init__(self, reason: str):
        super().__init__(f"stopped ({reason})")
        self.reason = reason


class TrackControl:
    """Thread-safe handle on a running track: stop or retarget it, change axis offsets, and see
    its phase (waiting, slewing, tracking, done)."""

    def __init__(self, on_phase=None):
        self._lock = threading.Lock()
        self._stop: str | None = None
        self._resend = False
        self._d_az = 0.0
        self._d_el = 0.0
        self.phase = "idle"
        self._on_phase = on_phase

    def stop(self, reason: str = "stop") -> None:
        if reason not in ("stop", "hold", "retarget"):
            raise ValueError(reason)
        with self._lock:
            self._stop = reason

    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop

    def set_axis_offsets(self, d_az: float, d_el: float) -> None:
        with self._lock:
            self._d_az, self._d_el = d_az, d_el
            self._resend = True

    def request_resend(self) -> None:
        """Re-send the buffered future points (e.g. a satellite correction changed)."""
        with self._lock:
            self._resend = True

    def axis_offsets(self) -> tuple[float, float]:
        with self._lock:
            return self._d_az, self._d_el

    def take_resend(self) -> bool:
        with self._lock:
            r, self._resend = self._resend, False
            return r

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        if self._on_phase:
            self._on_phase(phase)


def track(
    client: MountClient,
    plan: Plan,
    tracking: Tracking,
    disable_after: bool = True,
    stop_flag=None,
    control: TrackControl | None = None,
    wait_lead_s: float = 90.0,
) -> dict:
    """Runs a plan to its end: waits until shortly before it starts, enables, slews, streams.
    Returns statistics. Raises TrackAborted (drives disabled) on faults, trips and errors, and
    TrackStopped when stopped through `control` or `stop_flag` (the CLI's Ctrl-C)."""
    control = control or TrackControl()
    step = int(NS / tracking.point_rate_hz)

    def check_stop():
        reason = control.stop_reason() or ("stop" if stop_flag and stop_flag() else None)
        if reason:
            raise TrackStopped(reason)

    try:
        # Wait until shortly before the plan starts (e.g. a satellite pass later tonight).
        if plan.start_ns - time.time_ns() > wait_lead_s * NS:
            control.set_phase("waiting")
            while plan.start_ns - time.time_ns() > wait_lead_s * NS:
                check_stop()
                time.sleep(0.2)

        for axis in AXES:
            ok, msg = client.enable(axis)
            if not ok:
                raise TrackAborted(f"enable {axis} rejected: {msg}")
        deadline = time.monotonic() + 5.0
        while True:
            check_stop()
            tel = client.telemetry(fresh=True)
            states = {a.axis: a.state for a in tel.axes}
            if all(states.get(a) == mpb.DRIVE_STATE_OPERATION_ENABLED for a in AXES):
                break
            if time.monotonic() > deadline:
                raise TrackAborted(f"drives did not enable: {states}")

        start = {a.axis: a.cmd_position_rad for a in tel.axes if a.axis in AXES}
        slew = plan_slew(plan, start, time.time_ns(), tracking, offsets=control.axis_offsets())
        if slew.t_join_ns >= plan.end_ns:
            raise TrackAborted("cannot reach the target before the plan ends")
        log.info(
            "slew %.1f s, joining the plan %.1f s after its start",
            (slew.t_join_ns - slew.t0_ns) / NS,
            (slew.t_join_ns - plan.start_ns) / NS,
        )
        control.set_phase("slewing")
        client.clear_trajectory()
        client.send_points(slew.points(step))

        next_t = slew.t_join_ns + step
        # max_cmd_plan_rad: how far mount-rt's command strays from the plan while its limiter is
        # not intervening (interpolation and timing). limited_samples counts samples where it
        # did, e.g. recovering from missed cycles. max_cmd_act_rad: how far the axes lag their
        # command.
        stats = {"limited_samples": 0, "max_cmd_act_rad": 0.0, "max_cmd_plan_rad": 0.0, "samples": 0}
        offsets_changed_ns = 0
        while True:
            check_stop()
            now = time.time_ns()
            if control.take_resend() and now > slew.t_join_ns:
                # Replace buffered future points (mount-rt drops those at/after the first new one).
                next_t = max(now + 300_000_000, slew.t_join_ns + step)
                offsets_changed_ns = now
            if control.phase == "slewing" and now >= slew.t_join_ns:
                control.set_phase("tracking")
            d_az, d_el = control.axis_offsets()
            horizon = min(now + int(tracking.lookahead_s * NS), plan.end_ns)
            if next_t <= horizon:
                client.send_points(plan_points(plan, next_t, horizon, step, d_az, d_el))
                next_t = horizon + step
            tel = client.telemetry(fresh=True)
            tracking_phase = (
                slew.t_join_ns + NS < tel.t_ns < plan.end_ns - NS and tel.t_ns > offsets_changed_ns + 2 * NS
            )
            if tracking_phase:
                # mount-rt's cmd_position is for t + its command lead (1 ms by default).
                pa, pe = plan.mech(np.int64(tel.t_ns + 1_000_000))
                planned = {"az": pa[0] + d_az, "el": pe[0] + d_el}
            if tel.tripped:
                raise TrackAborted("following-error trip in mount-rt")
            for a in tel.axes:
                if a.axis not in AXES:
                    continue
                if a.state != mpb.DRIVE_STATE_OPERATION_ENABLED:
                    raise TrackAborted(f"{a.axis} left Operation Enabled ({mpb.DriveState.Name(a.state)})")
                if tracking_phase:
                    stats["samples"] += 1
                    stats["max_cmd_act_rad"] = max(stats["max_cmd_act_rad"], abs(a.cmd_position_rad - a.act_position_rad))
                    if a.flags & 1:
                        stats["limited_samples"] += 1
                    else:
                        stats["max_cmd_plan_rad"] = max(stats["max_cmd_plan_rad"], abs(a.cmd_position_rad - planned[a.axis]))
            if now > plan.end_ns + NS // 2:
                break
            time.sleep(0.1)
    except TrackStopped as e:
        if e.reason == "stop":
            _safe(client.disable)
        elif e.reason == "hold":
            _safe(client.clear_trajectory)  # mount-rt decelerates and holds
        control.set_phase("done")
        raise
    except (TrackAborted, MountError, KeyboardInterrupt):
        _safe(client.disable)
        control.set_phase("done")
        raise
    if disable_after:
        client.disable()
    control.set_phase("done")
    return stats


def _safe(fn) -> None:
    try:
        fn()
    except MountError as e:  # mount-rt gone: nothing more to do, the trajectory will run out
        log.warning("%s", e)
