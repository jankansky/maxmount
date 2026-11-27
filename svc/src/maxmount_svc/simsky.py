# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulated narrow-field camera's view of the sky while the mount tracks: where the target
really is on the sensor, given where the mount points (mount telemetry) and what it tracks
(svc/status). The "truth" has its own errors (ZwoSettings.sim_*): a TLE along-track and
cross-track error for satellites and a pointing error for every target, and its own sensor
orientation, so the camera loop has something real to find and correct.

This is deliberately independent of the loop's own geometry code: directions are compared as
vectors (targets.SatelliteTarget for the truth, the axis angles for the boresight), and the
offset is projected on the directions in which the two axes move the boresight.
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from . import astro
from .closedloop import CameraGeometry
from .config import SystemSettings
from .proto import mpb
from .targets import SatelliteTarget

ARCSEC = math.pi / 180 / 3600
NS = 1_000_000_000


class SimSky:
    def __init__(self, settings: SystemSettings, client):
        z = settings.zwo
        self.site = settings.site
        self.client = client
        self.cfg = z
        self.truth = CameraGeometry.from_config(z.sim_arcsec_per_px, z.sim_rotation_deg, bool(z.sim_flip))
        self._lock = threading.Lock()
        self._status: mpb.ServiceStatus | None = None
        self._sat: SatelliteTarget | None = None
        self._sub = client.session.declare_subscriber(client.key("svc/status"), self._on_status)

    def close(self) -> None:
        try:
            self._sub.undeclare()
        except Exception:  # noqa: BLE001
            pass

    def _on_status(self, sample) -> None:
        st = mpb.ServiceStatus()
        try:
            st.ParseFromString(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._status = st
            if st.target_kind == "tle" and st.tle_line1 and self.site is not None:
                if self._sat is None or self._sat.line1 != st.tle_line1 or self._sat.line2 != st.tle_line2:
                    self._sat = SatelliteTarget(self.site, st.tle_line1, st.tle_line2, name="truth",
                                                time_offset_s=self.cfg.sim_time_error_s,
                                                cross_track_rad=self.cfg.sim_cross_track_arcsec * ARCSEC)
            else:
                self._sat = None

    def _true_azel(self, st: mpb.ServiceStatus, t_ns: int) -> tuple[float, float] | None:
        with self._lock:
            sat = self._sat
        if sat is not None:
            az, el = sat.observed(np.int64(t_ns))
            az, el = float(az), float(el)
        elif st.target_name:
            dt = (t_ns - st.t_ns) / NS  # slow targets: extrapolate the status linearly
            az = math.radians(st.target_az_deg + st.target_az_rate_deg_s * dt)
            el = math.radians(st.target_el_deg + st.target_el_rate_deg_s * dt)
        else:
            return None
        el2 = el + self.cfg.sim_el_error_arcsec * ARCSEC
        az2 = az + self.cfg.sim_az_error_arcsec * ARCSEC / max(math.cos(el), 0.05)
        return az2, el2

    def offset_px(self, t_ns: int | None = None) -> tuple[float, float, float, float] | None:
        """The target's offset from the boresight (unbinned px, sensor x right, y down) and the
        star field's (the predicted target's, which the sky moves with), or None when the
        mount is not tracking anything."""
        t_ns = time.time_ns() if t_ns is None else t_ns
        with self._lock:
            st = self._status
        if st is None or st.state not in (mpb.SERVICE_STATE_TRACKING, mpb.SERVICE_STATE_SLEWING):
            return None
        if t_ns - st.t_ns > 3 * NS:
            return None  # mount-svc went away
        try:
            tel = self.client.telemetry(timeout=0.01)
        except Exception:  # noqa: BLE001 - no telemetry: nothing to point with
            return None
        if abs(t_ns - tel.t_ns) > NS:
            return None
        ax = {a.axis: a for a in tel.axes}
        if "az" not in ax or "el" not in ax:
            return None
        dt = (t_ns - tel.t_ns) / NS
        a = ax["az"].act_position_rad + ax["az"].cmd_velocity_rad_s * dt
        e = ax["el"].act_position_rad + ax["el"].cmd_velocity_rad_s * dt
        true = self._true_azel(st, t_ns)
        if true is None:
            return None
        p = project(true, a, e, self.truth)
        if p is None:
            return None
        dt_st = (t_ns - st.t_ns) / NS
        pred = (math.radians(st.target_az_deg + st.target_az_rate_deg_s * dt_st),
                math.radians(st.target_el_deg + st.target_el_rate_deg_s * dt_st))
        f = project(pred, a, e, self.truth)
        f = (0.0, 0.0) if f is None else f
        return float(p[0]), float(p[1]), float(f[0]), float(f[1])


def project(true_azel, a: float, e: float, geom: CameraGeometry) -> np.ndarray | None:
    """A sky direction's offset from the boresight of mechanical axes (a, e), in unbinned
    sensor px through `geom`; None if it is far outside the field."""
    u = astro.azel_to_enu(*true_azel)
    b = np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])
    h = np.array([math.cos(a), -math.sin(a), 0.0])                                        # +azimuth axis
    g = np.array([-math.sin(a) * math.sin(e), -math.cos(a) * math.sin(e), math.cos(e)])  # +elevation axis
    if float(u @ b) < math.cos(math.radians(2)):
        return None
    d = u - b
    return geom.to_px(np.array([float(d @ h), float(d @ g)]))
