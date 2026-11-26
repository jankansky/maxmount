# SPDX-License-Identifier: GPL-3.0-or-later
"""ROI tracking: find the target in each video frame and steer the camera's region of interest
(ROI) so that the target stays centred while it moves across the sensor.

Detection (measure): the frame is summed in blocks (an even block size, so a Bayer mosaic's
colours average out), smoothed by a 3 x 3 box, and the brightest peak above the background
(median) by snr_min times the noise (MAD) is the target. Block sums favour an extended,
bright satellite over a star of similar peak brightness. Once locked, the search is gated
around the predicted position (a few percent of the ROI, plus the motion since the last frame
and the target's size), so a brighter object elsewhere does not steal the lock. A pick (a
click on the image) searches the same small radius around the point. The
centroid is the flux-weighted mean over the window around the peak that holds the target's
light (grown until its edges are background).

Control (RoiTracker): an alpha-beta filter on the target's sensor position gives position
and velocity. The ROI centre is placed on the predicted position one latency ahead. The ROI
moves only when the error exceeds a dead band and the previous move has taken effect: the
camera delivers latency_frames frames already in its pipeline with the old ROI position.

All positions are in unbinned sensor pixels (x right, y down from the sensor's top-left).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class Detection:
    x: float          # in the frame's pixels (0 = the centre of the first pixel)
    y: float
    flux: float       # above background, in block-summed frame units
    snr: float
    size_px: int      # frame pixels above the threshold (a size estimate)


def _box3(s: np.ndarray) -> np.ndarray:
    p = np.pad(s, 1, mode="edge")
    h, w = s.shape
    out = np.zeros_like(s)
    for dy in range(3):
        for dx in range(3):
            out += p[dy:dy + h, dx:dx + w]
    return out


def block_size(width: int, height: int, target: int = 160) -> int:
    """An even block size giving a reduced image about `target` blocks across."""
    b = max(2, int(max(width, height) / target))
    return b + (b % 2)


def measure(frame: np.ndarray, near: tuple[float, float] | None = None, gate: float | None = None,
            snr_min: float = 8.0, block: int | None = None) -> Detection | None:
    """The target in one frame (H x W raw or mono, or H x W x 3), searched within `gate`
    frame pixels of `near` (frame pixels) or over the whole frame; None if nothing stands
    out by snr_min."""
    a = frame if frame.ndim == 2 else frame.sum(axis=2)
    h, w = a.shape
    bs = block or block_size(w, h)
    hb, wb = h // bs, w // bs
    if hb < 3 or wb < 3:
        return None
    s = a[:hb * bs, :wb * bs].reshape(hb, bs, wb, bs).sum(axis=(1, 3), dtype=np.float32)
    s3 = _box3(s)
    bg3 = float(np.median(s3))
    noise3 = 1.4826 * float(np.median(np.abs(s3 - bg3))) + 1e-3
    y0, y1, x0, x1 = 0, hb, 0, wb
    if near is not None and gate is not None:
        cx, cy, r = (near[0] + 0.5) / bs, (near[1] + 0.5) / bs, gate / bs + 1
        x0, x1 = max(int(cx - r), 0), min(int(math.ceil(cx + r)), wb)
        y0, y1 = max(int(cy - r), 0), min(int(math.ceil(cy + r)), hb)
        if x0 >= x1 or y0 >= y1:
            return None
    sub = s3[y0:y1, x0:x1]
    k = int(np.argmax(sub))
    py, px = y0 + k // sub.shape[1], x0 + k % sub.shape[1]
    snr = (float(s3[py, px]) - bg3) / noise3
    if snr < snr_min:
        return None
    # The target's window: grow until its border is background.
    bg = float(np.median(s))
    noise = 1.4826 * float(np.median(np.abs(s - bg))) + 1e-3
    thr = bg + 3 * noise
    r = 2
    while True:
        wy0, wy1, wx0, wx1 = max(py - r, 0), min(py + r + 1, hb), max(px - r, 0), min(px + r + 1, wb)
        win = s[wy0:wy1, wx0:wx1]
        edge = max(win[0].max(), win[-1].max(), win[:, 0].max(), win[:, -1].max())
        if edge < thr or r >= 40 or (wy0 == 0 and wx0 == 0 and wy1 == hb and wx1 == wb):
            break
        r += 2
    wts = np.clip(win - thr, 0, None)
    tot = float(wts.sum())
    if tot <= 0:
        return None
    yy, xx = np.mgrid[wy0:wy1, wx0:wx1]
    cyb, cxb = float((wts * yy).sum() / tot), float((wts * xx).sum() / tot)
    return Detection(x=(cxb + 0.5) * bs - 0.5, y=(cyb + 0.5) * bs - 0.5, flux=float(np.clip(win - bg, 0, None).sum()),
                     snr=snr, size_px=int((wts > 0).sum()) * bs * bs)


class RoiTracker:
    """Keeps the ROI on the target. update() takes each frame with the ROI origin it was read
    with and returns where the ROI centre should go (or None to leave it)."""

    def __init__(self, latency_frames: int = 2, snr_min: float = 8.0, gate_frac: float = 0.06,
                 deadband_frac: float = 0.03, lost_after: int = 15, alpha: float = 0.6, beta: float = 0.2):
        self.latency_frames = int(latency_frames)
        self.snr_min, self.gate_frac, self.deadband_frac = snr_min, gate_frac, deadband_frac
        self.lost_after, self.alpha, self.beta = lost_after, alpha, beta
        self.state = "off"           # off | acquiring | locked | lost
        self.pick: tuple[float, float] | None = None  # sensor px: acquire the object nearest this
        self.pos: np.ndarray | None = None  # sensor px
        self.vel = np.zeros(2)       # sensor px/s
        self.t_ns = 0
        self.misses = 0
        self.moves = 0
        self.last: Detection | None = None   # in frame px
        self.last_sensor: tuple[float, float] | None = None
        self._frames_since_move = 10**9

    def start(self, pick: tuple[float, float] | None = None) -> None:
        self.state, self.pick, self.pos, self.vel = "acquiring", pick, None, np.zeros(2)
        self.misses, self.last, self.last_sensor = 0, None, None

    def stop(self) -> None:
        self.state, self.pick, self.last = "off", None, None

    def reacquire(self) -> None:
        """After a change of ROI size or binning: look again near the last position."""
        if self.state != "off":
            p = tuple(self.pos) if self.pos is not None else self.pick
            self.start(p)

    def update(self, frame: np.ndarray, origin: tuple[int, int], binning: int, t_ns: int,
               roi_center: tuple[float, float], frame_dt_s: float) -> tuple[float, float] | None:
        """frame: this frame; origin: its ROI origin (binned px); t_ns: its mid-exposure time;
        roi_center: the current (requested) ROI centre, sensor px; frame_dt_s: the frame
        interval. Returns a new ROI centre (sensor px) or None."""
        if self.state == "off":
            return None
        self._frames_since_move += 1
        b = binning
        h, w = frame.shape[:2]
        to_frame = lambda p: ((p[0] / b) - origin[0], (p[1] / b) - origin[1])  # noqa: E731
        dt = (t_ns - self.t_ns) / 1e9 if self.t_ns else 0.0
        near, gate = None, None
        if self.state == "locked" and self.pos is not None:
            pred = self.pos + self.vel * dt
            near = to_frame(pred)
            # Wide enough for the motion since the last frame and for the target's own size (an
            # extended target's brightest part can be far from its centroid).
            radius = math.sqrt(self.last.size_px / math.pi) if self.last else 0.0
            gate = max(24.0, self.gate_frac * min(w, h)) + float(np.hypot(*self.vel)) * dt / b * 2 + radius
        elif self.pick is not None and self.state == "acquiring":
            near, gate = to_frame(self.pick), max(24.0, self.gate_frac * min(w, h))
        d = measure(frame, near, gate, self.snr_min)
        if d is None:
            self.misses += 1
            if self.state == "locked" and self.misses >= self.lost_after:
                self.state = "lost"
            return None
        self.misses, self.last = 0, d
        z = np.array([(origin[0] + d.x) * b, (origin[1] + d.y) * b])
        self.last_sensor = (float(z[0]), float(z[1]))
        if self.state != "locked" or self.pos is None or dt <= 0:
            self.pos, self.vel, self.state = z, np.zeros(2), "locked"
        else:
            pred = self.pos + self.vel * dt
            r = z - pred
            self.pos = pred + self.alpha * r
            self.vel = self.vel + self.beta * r / dt
        self.t_ns = t_ns
        # Steer: lead by the time until a move takes effect.
        if self._frames_since_move <= self.latency_frames:
            return None
        lead = (self.latency_frames + 1) * frame_dt_s
        want = self.pos + self.vel * lead
        dead = max(4.0, self.deadband_frac * min(w, h)) * b
        if float(np.hypot(*(want - np.asarray(roi_center)))) < dead:
            return None
        self._frames_since_move = 0
        self.moves += 1
        return float(want[0]), float(want[1])

    def status(self, origin: tuple[int, int], size: tuple[int, int], binning: int) -> dict:
        out = {"roi_tracking": self.state != "off", "track_state": self.state, "roi_moves": self.moves,
               "track_x": float("nan"), "track_y": float("nan"), "track_err_x": float("nan"), "track_err_y": float("nan"),
               "track_snr": 0.0, "track_speed": float(np.hypot(*self.vel)) / binning if self.state == "locked" else 0.0}
        if self.last_sensor is not None and self.state == "locked":
            x, y = self.last_sensor[0] / binning - origin[0], self.last_sensor[1] / binning - origin[1]
            out.update(track_x=x, track_y=y, track_err_x=x - size[0] / 2, track_err_y=y - size[1] / 2,
                       track_snr=self.last.snr if self.last else 0.0)
        return out
