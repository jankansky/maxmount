# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end: mount-svc plans and streams a trajectory to a real `mount-rt --serve` running the
simulated mount, over Zenoh on loopback TCP. Skipped unless a mount-rt built with
-DMAXMOUNT_WITH_ZENOH=ON is found (MAXMOUNT_RT, or build*/rt/mount-rt in the repo)."""
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
import yaml

from maxmount_svc import planner, tracker
from maxmount_svc.config import load_system
from maxmount_svc.mount import MountClient
from maxmount_svc.proto import mpb

from .conftest import DEG, NS, GreatCircleTarget

REPO = Path(__file__).resolve().parents[2]


def _mount_rt():
    candidates = [os.environ.get("MAXMOUNT_RT")] + sorted(str(p) for p in REPO.glob("build*/rt/mount-rt"))
    for c in candidates:
        if c and Path(c).is_file() and _has_zenoh(c):
            return c
    return None


def _has_zenoh(binary):
    # A build without Zenoh refuses --serve immediately.
    p = subprocess.run([binary, "--serve", "--period-us", "abc"], capture_output=True, text=True, timeout=10)
    return "MAXMOUNT_WITH_ZENOH" not in (p.stdout + p.stderr)


@pytest.fixture
def served(tmp_path):
    binary = _mount_rt()
    if not binary:
        pytest.skip("no mount-rt built with Zenoh")
    shutil.copytree(REPO / "config" / "drives", tmp_path / "drives")
    sys_dir = tmp_path / "systems" / "e2e"
    sys_dir.mkdir(parents=True)
    doc = yaml.safe_load((REPO / "config/systems/sim-altaz/system.yaml").read_text())
    port = random.randint(20000, 40000)
    doc["zenoh"] = {"site": f"e2e{os.getpid()}", "listen": [f"tcp/127.0.0.1:{port}"], "multicast_scouting": False}
    (sys_dir / "system.yaml").write_text(yaml.safe_dump(doc))
    proc = subprocess.Popen(
        [binary, "--system", str(sys_dir / "system.yaml"), "--serve", "--rt-prio", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    settings = load_system(sys_dir / "system.yaml")
    settings.zenoh.listen = []
    settings.zenoh.connect = [f"tcp/127.0.0.1:{port}"]
    try:
        with MountClient(settings.zenoh) as client:
            client.telemetry(timeout=10.0)
            yield client, settings
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_slew_and_track_a_moving_target(served):
    client, settings = served
    t0 = time.time_ns() + 1 * NS
    target = GreatCircleTarget(t0, a0=60 * DEG, d=30 * DEG, rate=2.0 * DEG, start=20 * DEG)
    near = client.axis("az").cmd_position_rad
    plan = planner.plan_mode("normal", target, t0, t0 + 25 * NS, settings.az, settings.el, settings.tracking, near_az=near)
    assert plan.feasible, plan.problems

    before = client.telemetry(fresh=True).loop
    stats = tracker.track(client, plan, settings.tracking, disable_after=False)
    after = client.telemetry(fresh=True).loop
    assert stats["samples"] > 50
    # Where mount-rt's limiter is not intervening, its command matches the plan (Hermite
    # interpolation of 20 Hz points).
    assert np.degrees(stats["max_cmd_plan_rad"]) * 3600 < 10
    # Missed cycles make the limiter catch up (and say so). How often depends on the host: none
    # on the PREEMPT_RT Pi, some on a desktop OS, constantly in a Docker Desktop VM. Only judge
    # the limiter's share when the loop kept time.
    missed = (after.overruns - before.overruns) / max(1, after.cycles - before.cycles)
    if missed < 0.01:
        assert stats["limited_samples"] < 0.5 * stats["samples"], f"missed {missed:.1%} of cycles"
    assert np.degrees(stats["max_cmd_act_rad"]) < 0.05

    # The plan ends with the target moving at 2 deg/s: when the points run out mount-rt
    # decelerates at the axis limit and holds (starved), a little past the last point.
    tel = client.telemetry(fresh=True)
    axes = {a.axis: a for a in tel.axes}
    assert axes["az"].flags & 2
    a_end, _ = plan.mech(plan.end_ns)
    v_end = (plan.mech(plan.end_ns)[0] - plan.mech(plan.end_ns - NS // 10)[0])[0] * 10
    max_overshoot = v_end**2 / (2 * settings.az.max_acc)  # stopping at the axis acceleration
    # Where exactly it stops depends on when the last points ran out (host timing); the safety
    # property is that the stop is bounded. If the loop was missing cycles, the axis may still
    # have been catching up (moving faster than the target) when the points ran out, so the
    # bound only holds when it kept time. The C++ limiter tests cover the stop itself.
    if missed < 0.01:
        assert abs(axes["az"].act_position_rad - a_end[0]) <= max_overshoot + 0.02 * DEG

    ok, msg = client.disable()
    assert ok, msg
    deadline = time.monotonic() + 5
    while client.telemetry(fresh=True).enabled and time.monotonic() < deadline:
        pass
    assert not client.telemetry(fresh=True).enabled


def test_unknown_axis_is_rejected(served):
    client, _ = served
    ok, msg = client.enable("dec")
    assert not ok and "unknown axis" in msg
    st = client.telemetry().axes[0].state
    assert st != mpb.DRIVE_STATE_OPERATION_ENABLED
