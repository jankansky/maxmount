# SPDX-License-Identifier: GPL-3.0-or-later
"""Zenoh client for mount-rt (keys in schemas/zenoh-keys.md)."""
from __future__ import annotations

import json
import logging
import threading
import time

import zenoh

from .config import ZenohSettings
from .proto import mpb


class MountError(RuntimeError):
    pass


class MountClient:
    def __init__(self, settings: ZenohSettings):
        cfg = zenoh.Config()
        cfg.insert_json5("mode", json.dumps(settings.mode if settings.mode != "router" else "peer"))
        if settings.connect:
            cfg.insert_json5("connect/endpoints", json.dumps(settings.connect))
        cfg.insert_json5("scouting/multicast/enabled", json.dumps(settings.multicast_scouting))
        self.prefix = f"maxmount/{settings.site}/"
        self._session = zenoh.open(cfg)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._telemetry: mpb.MountTelemetry | None = None
        self._received = 0
        self._sub = self._session.declare_subscriber(self.key("mount/telemetry"), self._on_telemetry)

    def key(self, suffix: str) -> str:
        return self.prefix + suffix

    @property
    def session(self) -> zenoh.Session:
        return self._session

    def close(self) -> None:
        # Shutting down must not fail: by now the drives are disabled or the trajectory will
        # run out. Zenoh can time out closing a session whose peers have already gone.
        for fn in (self._sub.undeclare, self._session.close):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                logging.getLogger(__name__).debug("zenoh close: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- telemetry ---------------------------------------------------------------------------
    def _on_telemetry(self, sample) -> None:
        m = mpb.MountTelemetry()
        try:
            m.ParseFromString(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001 - a malformed message must not kill the callback
            return
        with self._cv:
            self._telemetry = m
            self._received += 1
            self._cv.notify_all()

    def telemetry(self, timeout: float = 2.0, fresh: bool = False) -> mpb.MountTelemetry:
        """Latest telemetry; with fresh=True waits for a sample newer than the call."""
        deadline = time.monotonic() + timeout
        with self._cv:
            start = self._received
            while self._telemetry is None or (fresh and self._received == start):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MountError(f"no telemetry on {self.key('mount/telemetry')}: is mount-rt running with zenoh?")
                self._cv.wait(left)
            return self._telemetry

    def axis(self, name: str, timeout: float = 2.0):
        for a in self.telemetry(timeout).axes:
            if a.axis == name:
                return a
        raise MountError(f"mount-rt has no axis '{name}'")

    # -- commands ----------------------------------------------------------------------------
    def query(self, suffix: str, payload: bytes, timeout: float = 3.0, discovery_s: float = 5.0) -> bytes | None:
        """First reply payload of a Zenoh query, or None. A query that finds no queryable at all
        returns at once; right after start-up that can just mean routes are still being
        discovered, so it is retried for up to discovery_s. A reply, even a rejection, is
        never retried."""
        deadline = time.monotonic() + discovery_s
        while True:
            for reply in self._session.get(self.key(suffix), payload=payload, timeout=timeout):
                if reply.ok is not None:
                    return reply.ok.payload.to_bytes()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    def command(self, cmd: mpb.MountCommand, timeout: float = 3.0) -> tuple[bool, str]:
        out = self.query("mount/cmd", cmd.SerializeToString(), timeout)
        if out is None:
            raise MountError("no reply on mount/cmd: is mount-rt running?")
        r = mpb.CommandReply()
        r.ParseFromString(out)
        return r.accepted, r.message

    def _simple(self, field: str, axis: str = "") -> tuple[bool, str]:
        m = mpb.MountCommand()
        sub = getattr(m, field)
        if hasattr(sub, "axis"):
            sub.axis = axis
        else:
            sub.SetInParent()
        return self.command(m)

    def enable(self, axis: str = "") -> tuple[bool, str]:
        return self._simple("enable", axis)

    def disable(self, axis: str = "") -> tuple[bool, str]:
        return self._simple("disable", axis)

    def quick_stop(self) -> tuple[bool, str]:
        return self._simple("quick_stop")

    def fault_reset(self) -> tuple[bool, str]:
        return self._simple("fault_reset")

    def clear_trajectory(self, axis: str = "") -> tuple[bool, str]:
        return self._simple("clear_trajectory", axis)

    # -- trajectories ------------------------------------------------------------------------
    def send_points(self, points: dict[str, list[tuple[int, float, float]]]) -> None:
        """points: axis -> [(t_ns, position_rad, velocity_rad_s), ...] with increasing t."""
        u = mpb.TrajectoryUpdate()
        for name, pts in points.items():
            if not pts:
                continue
            at = u.axes.add()
            at.axis = name
            for t, p, v in pts:
                q = at.points.add()
                q.t_ns = int(t)
                q.position_rad = float(p)
                q.velocity_rad_s = float(v)
        self._session.put(self.key("mount/trajectory"), u.SerializeToString())
