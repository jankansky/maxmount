# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed-loop optical tracking: the narrow-field camera's centroids (camera/<id>/centroid)
turned into tracking corrections.

Frames. A centroid is the target's offset from the boresight in unbinned sensor pixels
p = (dx, dy). The camera is fixed to the optical tube, so the sensor keeps one orientation
relative to the mount's axes. CameraGeometry maps p to the "axis frame":

    q = M p,    q = (cross-elevation, elevation) in radians

with q_x = delta_a * cos(e) and q_y = delta_e for small moves delta_a, delta_e of the
mechanical axes at mechanical elevation e (e > 90 deg on the flipped branch, where cos(e) < 0;
the same M serves both branches). q is how far the boresight has to move to put the target on it.
M comes from the configured scale, rotation and flip, or from a calibration: two small axis
moves and the target's shift on the sensor measure it, parity included.

Corrections. Each update solves J x = q for the correction x, where J's columns are the
change in q per unit of each correction, taken numerically from the plan itself (so signs
follow the target code, not a hand derivation):

    satellites on an exact plan   time offset (s) and cross-track (rad)
    satellites over the top       time offset and the azimuth axis (cross-track barely moves
                                  the commanded axes there)
    other targets                 the azimuth and elevation axis offsets

and adds gain * x to the live offsets. The next update waits until the change has reached the
axes (settle_s) and averages the centroids after that (avg_s): the loop runs at about 1 Hz,
fast enough for TLE errors, which change slowly over a pass.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ARCSEC = math.pi / 180 / 3600
NS = 1_000_000_000
H_NS = 200_000_000  # numerical-derivative step for the plan Jacobian


# -- geometry -------------------------------------------------------------------------------------
@dataclass
class CameraGeometry:
    m: np.ndarray                 # 2 x 2: unbinned px -> axis frame (rad)
    source: str = "config"        # config | calibrated <UTC time>

    @classmethod
    def from_config(cls, arcsec_per_px: float, rotation_deg: float = 0.0, flip: bool = False) -> CameraGeometry:
        """Sensor +x at rotation_deg from the +azimuth (cross-elevation) direction, towards
        +elevation; sensor +y (down in the image) is then 90 deg further clockwise, i.e. towards
        -elevation at rotation 0, unless flip."""
        r = math.radians(rotation_deg)
        rot = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
        parity = np.diag([1.0, 1.0 if flip else -1.0])
        return cls(arcsec_per_px * ARCSEC * rot @ parity, "config")

    def to_axis(self, dx: float, dy: float) -> np.ndarray:
        return self.m @ np.array([dx, dy], dtype=float)

    def to_px(self, q) -> np.ndarray:
        return np.linalg.solve(self.m, np.asarray(q, dtype=float))

    def describe(self) -> dict:
        """Scale ("/px), rotation of sensor +x from +azimuth (deg) and whether it is mirrored
        relative to the configured convention."""
        det = float(np.linalg.det(self.m))
        flip = det > 0  # from_config's unflipped form has det < 0
        m = self.m @ np.diag([1.0, 1.0 if flip else -1.0])  # a rotation times the scale
        scale = math.sqrt(abs(det)) / ARCSEC
        rot = math.degrees(math.atan2(m[1, 0], m[0, 0]))
        return {"arcsec_per_px": round(scale, 4), "rotation_deg": round(rot, 2), "flip": flip, "source": self.source}

    # Calibrations are kept per system and camera, like the active imaging profile.
    @staticmethod
    def state_path(system: str, camera_id: str, state_dir: Path | None = None) -> Path:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        return (state_dir or base / "maxmount") / f"{system}-camera-{camera_id}.json"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"m": self.m.tolist(), "source": self.source, **self.describe()}, indent=1))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> CameraGeometry | None:
        try:
            d = json.loads(path.read_text())
            m = np.array(d["m"], dtype=float)
            if m.shape != (2, 2) or abs(np.linalg.det(m)) < 1e-30:
                return None
            return cls(m, str(d.get("source", "calibrated")))
        except (OSError, ValueError, KeyError, TypeError):
            return None


def calibrate(p0, t0: float, p1, t1: float, p2, t2: float, p3, t3: float, q1, q2, source: str) -> CameraGeometry:
    """Geometry from four mean centroids (unbinned px) and their times: p0 before, p1 with the
    boresight moved by q1 (axis frame, rad; a cross-elevation step), p2 moved by q2 (an
    elevation step), p3 after. The baseline at t1 and t2 is interpolated between p0 and p3
    (the target drifts on the sensor while uncorrected). Moving the boresight by q moves the
    target on the sensor by -M^-1 q, so M = -[q1 q2] [d1 d2]^-1."""
    p0, p1, p2, p3 = (np.asarray(p, dtype=float) for p in (p0, p1, p2, p3))

    def base(t):
        return p0 + (p3 - p0) * ((t - t0) / (t3 - t0) if t3 > t0 else 0.0)

    d = np.column_stack([p1 - base(t1), p2 - base(t2)])
    if abs(np.linalg.det(d)) < 1e-6 or min(np.linalg.norm(d, axis=0)) < 3.0:
        raise ValueError(f"the target hardly moved on the sensor ({np.linalg.norm(d, axis=0).round(1).tolist()} px)")
    return CameraGeometry(-np.column_stack([q1, q2]) @ np.linalg.inv(d), source)


# -- the controller ---------------------------------------------------------------------------------
@dataclass
class Measurement:
    t_ns: int
    p: np.ndarray        # unbinned px from the boresight
    snr: float


def axis_frame(da: float, de: float, e: float) -> np.ndarray:
    return np.array([da * math.cos(e), de])


def jacobian(plan, target, kind: str, t_ns: int) -> tuple[np.ndarray, tuple[str, str], float]:
    """Columns: the change in q per unit of each correction at t, and their names; plus the
    mechanical elevation. kind: the service's target kind (tle, radec, altaz, aircraft)."""
    a0, e0 = (float(v[0]) for v in plan.mech(np.int64(t_ns)))
    cos_e = math.cos(e0)
    az_col = np.array([cos_e, 0.0])  # 1 rad on the azimuth axis
    if kind == "tle" and target is not None and hasattr(target, "time_offset_s"):
        def moved(**kw):
            t2 = dataclasses.replace(target, **kw)
            a, e = plan.mech_with(t2, np.int64(t_ns))
            return axis_frame(float(a[0]) - a0, float(e[0]) - e0, e0)

        dt = H_NS / NS
        col_t = moved(time_offset_s=target.time_offset_s + dt) / dt
        if plan.mode in ("normal", "flipped"):
            dc = 20 * ARCSEC
            col_c = moved(cross_track_rad=target.cross_track_rad + dc) / dc
            return np.column_stack([col_t, col_c]), ("time_offset_s", "cross_track_rad"), e0
        return np.column_stack([col_t, az_col]), ("time_offset_s", "az_rad"), e0
    return np.column_stack([az_col, [0.0, 1.0]]), ("az_rad", "el_rad"), e0


def solve(j: np.ndarray, q: np.ndarray, min_cos: float = 0.05) -> np.ndarray:
    """x with J x = q; least squares with small singular values dropped (near the zenith the
    azimuth axis barely moves the boresight, and a satellite's motion can line up with one of
    the columns)."""
    u, s, vt = np.linalg.svd(j)
    keep = s > max(min_cos * s[0], 1e-12) if s[0] > 0 else np.zeros_like(s, bool)
    inv = np.where(keep, 1.0 / np.where(keep, s, 1.0), 0.0)
    return vt.T @ (inv * (u.T @ q))


@dataclass
class LoopSettings:
    gain: float = 0.5
    settle_s: float = 0.6         # after a correction, until the axes have it
    avg_s: float = 0.25           # centroids averaged per update
    min_samples: int = 3
    max_age_s: float = 0.5        # centroids older than this: the camera lost the target
    min_snr: float = 8.0
    max_step_arcsec: float = 120.0  # largest single correction (the boresight's move)
    max_total_arcsec: float = 600.0  # the loop opens if its corrections add up to more (a wrong lock)
    max_spread_px: float = 30.0   # centroids scattered more than this: the mount is still settling
    max_step_px: float = 20.0     # largest image move per correction: the ROI tracker's search
                                  # window must still hold the target after the jump


@dataclass
class LoopState:
    on: bool = False
    state: str = "off"            # off | waiting | correcting | settling
    note: str = ""
    updates: int = 0
    err: np.ndarray = field(default_factory=lambda: np.zeros(2))  # last q (rad), axis frame
    err_px: np.ndarray = field(default_factory=lambda: np.zeros(2))
    t_err_ns: int = 0
    first_err: float = 0.0
    worst_streak: int = 0
    moved: np.ndarray = field(default_factory=lambda: np.zeros(2))  # sum of the boresight moves (rad)


class LoopController:
    """Collects centroids and decides when there is a fresh average to correct on. The service
    computes and applies the correction."""

    def __init__(self, settings: LoopSettings | None = None):
        self.s = settings or LoopSettings()
        self.buf: list[Measurement] = []
        self.ready_ns = 0         # centroids from before this do not show the last correction
        self.st = LoopState()

    def reset(self, now_ns: int) -> None:
        self.buf.clear()
        self.ready_ns = now_ns
        self.st.updates, self.st.first_err, self.st.worst_streak = 0, 0.0, 0
        self.st.moved = np.zeros(2)

    def add(self, m: Measurement) -> None:
        if m.snr < self.s.min_snr or m.t_ns < self.ready_ns:
            return
        self.buf.append(m)
        if len(self.buf) > 2000:
            del self.buf[:1000]

    def average(self, now_ns: int) -> tuple[np.ndarray, int] | None:
        """The mean centroid and its mean time once avg_s of centroids since ready_ns are in;
        None while waiting. Old centroids (camera lost the target) are dropped."""
        self.buf = [m for m in self.buf if now_ns - m.t_ns < self.s.max_age_s * NS + self.s.avg_s * NS]
        if not self.buf:
            return None
        if self.buf[-1].t_ns - self.buf[0].t_ns < self.s.avg_s * NS * 0.8 or len(self.buf) < self.s.min_samples:
            return None
        if now_ns - self.buf[-1].t_ns > self.s.max_age_s * NS:
            return None
        ps = np.array([m.p for m in self.buf])
        med = np.median(ps, axis=0)
        if float(np.max(np.linalg.norm(ps - med, axis=1))) > self.s.max_spread_px:
            # Still moving on the sensor (the mount arriving, or the lock jumping): start over.
            self.buf = self.buf[len(self.buf) // 2:]
            return None
        p = np.mean(ps, axis=0)
        t = int(np.mean([m.t_ns for m in self.buf]))
        return p, t

    def applied(self, now_ns: int) -> None:
        self.buf.clear()
        self.ready_ns = now_ns + int(self.s.settle_s * NS)
        self.st.updates += 1

    def diverging(self, err_rad: float) -> bool:
        """True if the error keeps growing: the geometry is probably wrong (a mirrored or
        rotated sensor)."""
        if self.st.updates <= 1 or self.st.first_err == 0.0:
            self.st.first_err = max(self.st.first_err, err_rad)
            return False
        if err_rad > 1.5 * self.st.first_err and err_rad > 20 * ARCSEC:
            self.st.worst_streak += 1
        else:
            self.st.worst_streak = 0
        return self.st.worst_streak >= 3


def correction(j: np.ndarray, names: tuple[str, str], q: np.ndarray, max_step_rad: float) -> np.ndarray:
    """The full correction for an error q (before the loop gain). The time column (rad per s
    of along-track motion) is scaled to unit size first so that solve's cut-off compares
    directions, not units; the axis columns keep their size, so the azimuth axis near the
    zenith (cos e -> 0) is still dropped rather than driven hard."""
    scale = np.array([max(float(np.linalg.norm(j[:, k])), 1e-12) if n == "time_offset_s" else 1.0
                      for k, n in enumerate(names)])
    x = solve(j / scale, q) / scale
    return clamp_step(x, j, max_step_rad)


def clamp_px(x: np.ndarray, j: np.ndarray, geom: CameraGeometry, max_px: float) -> np.ndarray:
    """Scales x down so the image moves at most max_px on the sensor."""
    move = float(np.linalg.norm(geom.to_px(j @ x)))
    return x * (max_px / move) if move > max_px else x


def clamp_step(x: np.ndarray, j: np.ndarray, max_rad: float) -> np.ndarray:
    """Scales x down so the boresight move J x is at most max_rad."""
    move = float(np.linalg.norm(j @ x))
    return x * (max_rad / move) if move > max_rad else x


def now_ns() -> int:
    return time.time_ns()
