# SPDX-License-Identifier: GPL-3.0-or-later
"""mount-svc as a long-running Zenoh service (`mount-svc serve`).

Keys under maxmount/<site>/ (schemas/zenoh-keys.md, messages in schemas/maxmount/v1/target.proto):

    svc/target   query   TargetCommand -> TargetReply   track, go to, offsets, stop, jog
    svc/passes   query   PassesRequest -> PassesReply   upcoming passes of a TLE with plans
    svc/status   pub     ServiceStatus, 2 Hz
    camera/<closed_loop.camera>/centroid   sub   CameraCentroid (the camera loop)

Threads: Zenoh callbacks hand target commands to one request thread, which plans them and
owns every action on mount-rt; each track runs in a worker thread controlled through a
TrackControl. A new target retargets without disabling the drives. The reply to a track or
goto command is sent after planning, so it carries the chosen plan or the reason it was refused.
The camera loop (closedloop.py) runs in its own thread and changes the offsets like the GUI.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future

import numpy as np

from . import adsb, clock, closedloop, planner, pointfit, pointrun, skyframe, tracker
from .config import SystemSettings
from .mount import MountClient, MountError
from .planner import NS, Plan
from .pointing import PointingModel
from .proto import mpb
from .targets import FixedTarget, Sampled, SatelliteTarget, SgpError, SiderealTarget

log = logging.getLogger(__name__)
DEG = math.pi / 180
ARCSEC = DEG / 3600
MAS = ARCSEC / 1000

STATE = {
    "idle": mpb.SERVICE_STATE_IDLE,
    "waiting": mpb.SERVICE_STATE_WAITING,
    "slewing": mpb.SERVICE_STATE_SLEWING,
    "tracking": mpb.SERVICE_STATE_TRACKING,
    "error": mpb.SERVICE_STATE_ERROR,
}


def plan_summary(p: Plan | None) -> mpb.PlanSummary:
    s = mpb.PlanSummary()
    if p is None:
        return s
    s.mode = p.mode
    s.feasible = p.feasible
    s.problems.extend(p.problems)
    s.start_ns = p.start_ns
    s.end_ns = p.end_ns
    s.peak_az_deg_s = float(np.degrees(p.peak_vel[0]))
    s.peak_el_deg_s = float(np.degrees(p.peak_vel[1]))
    s.max_error_deg = float(np.degrees(p.max_error))
    return s


def _utc_hms(t_ns: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(t_ns / NS))


def _rejection(plans: list[Plan]) -> str:
    return "no feasible plan: " + "; ".join(f"{p.mode}: {', '.join(p.problems)}" for p in plans)


class Rejected(Exception):
    pass


class MountService:
    def __init__(self, settings: SystemSettings, client: MountClient, model: PointingModel | None = None):
        if settings.site is None:
            raise ValueError("mount-svc serve needs a site (system file `site:` section)")
        self.settings = settings
        self.client = client
        self.model = model or pointfit.load_model(settings)
        self.pointing = pointrun.PointingRun(self)
        self._requests: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._threads: list[threading.Thread] = []
        self._decls = []

        # Guarded by _lock.
        self._gen = 0  # increments per track; a worker only reports for its own generation
        self._state = "idle"
        self._message = "started"
        self._target = None
        self._target_kind = ""
        self._plan: Plan | None = None
        self._offsets = mpb.Offsets()
        self._control: tracker.TrackControl | None = None
        self._worker: threading.Thread | None = None
        self._path: list = []
        self._jog: dict | None = None  # axis, name, end_ns while a jog moves
        self._clock: clock.ClockStatus | None = None
        self._clock_checked = 0.0
        self._stop_note: dict[int, str] = {}  # why a track was stopped from inside (per generation)
        self._adsb = adsb.AdsbSource(settings.adsb.source)
        self._offsets_lock = threading.Lock()  # read-modify-write of the offsets (GUI, camera loop)

        # The camera loop; guarded by _loop_lock.
        cl = settings.closed_loop
        self._loop_lock = threading.Lock()
        self._loop = closedloop.LoopController(closedloop.LoopSettings(
            gain=cl.gain, settle_s=cl.settle_s, avg_s=cl.avg_s, min_snr=cl.min_snr, max_step_arcsec=cl.max_step_arcsec,
            max_total_arcsec=cl.max_total_arcsec, max_step_px=cl.max_step_px))
        self._geom_path = closedloop.CameraGeometry.state_path(settings.name, cl.camera)
        self._geom = closedloop.CameraGeometry.load(self._geom_path) or self._config_geometry()
        self._recent: list[closedloop.Measurement] = []  # locked centroids, the last few seconds
        self._last_centroid_ns = 0
        self._calibrating = False
        self._corrects = ""

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        s = self.client.session
        self._status_pub = s.declare_publisher(self.client.key("svc/status"))
        self._decls += [
            s.declare_queryable(self.client.key("svc/target"), self._on_target_query),
            s.declare_queryable(self.client.key("svc/passes"), self._on_passes_query),
            s.declare_queryable(self.client.key("svc/pointing"), self._on_pointing_query),
            s.declare_subscriber(self.client.key(f"camera/{self.settings.closed_loop.camera}/centroid"), self._on_centroid),
            self._status_pub,
        ]
        self.pointing.start_listening()
        for fn in (self._request_loop, self._status_loop, self._loop_run):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self._threads.append(t)
        log.info("mount-svc serving on %s", self.client.key("svc/*"))

    def close(self, disable: bool = True) -> None:
        self._closing.set()
        self.pointing._abort.set()
        with self._lock:
            control, worker = self._control, self._worker
        if control:
            control.stop("stop" if disable else "hold")
        if worker:
            worker.join(timeout=10)
        for d in self._decls:
            try:
                d.undeclare()
            except Exception:  # noqa: BLE001
                pass
        for t in self._threads:
            t.join(timeout=2)

    # -- Zenoh callbacks ---------------------------------------------------------------------
    def _on_target_query(self, query) -> None:
        reply = mpb.TargetReply()
        try:
            cmd = mpb.TargetCommand()
            cmd.ParseFromString(query.payload.to_bytes() if query.payload is not None else b"")
            fut: Future = Future()
            self._requests.put((cmd, fut))
            reply = fut.result(timeout=20)
        except Exception as e:  # noqa: BLE001 - always answer the query
            reply.accepted = False
            reply.message = f"{type(e).__name__}: {e}"
        query.reply(self.client.key("svc/target"), reply.SerializeToString())

    def _on_passes_query(self, query) -> None:
        try:
            req = mpb.PassesRequest()
            req.ParseFromString(query.payload.to_bytes() if query.payload is not None else b"")
            reply = self.passes(req)
        except Exception as e:  # noqa: BLE001
            reply = mpb.PassesReply(ok=False, message=f"{type(e).__name__}: {e}")
        query.reply(self.client.key("svc/passes"), reply.SerializeToString())

    def _on_pointing_query(self, query) -> None:
        try:
            req = mpb.PointingRequest()
            req.ParseFromString(query.payload.to_bytes() if query.payload is not None else b"")
            reply = self.pointing.handle(req)
        except Exception as e:  # noqa: BLE001
            log.exception("pointing request failed")
            reply = mpb.PointingReply(ok=False, message=f"{type(e).__name__}: {e}")
        query.reply(self.client.key("svc/pointing"), reply.SerializeToString())

    # -- request thread ----------------------------------------------------------------------
    def _request_loop(self) -> None:
        while not self._closing.is_set():
            try:
                cmd, fut = self._requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                fut.set_result(self.handle(cmd))
            except Rejected as e:
                fut.set_result(mpb.TargetReply(accepted=False, message=str(e)))
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                fut.set_result(mpb.TargetReply(accepted=False, message=f"{type(e).__name__}: {e}"))

    def handle(self, cmd) -> mpb.TargetReply:
        which = cmd.WhichOneof("command")
        if which is None:
            raise Rejected("empty TargetCommand")
        if which == "set_offsets":
            with self._offsets_lock:
                return self._set_offsets(cmd.set_offsets)
        if which == "add_offsets":
            self._add_offsets(cmd.add_offsets)
            return mpb.TargetReply(accepted=True, message="offsets changed")
        if which == "closed_loop":
            return self._closed_loop(cmd.closed_loop.on)
        if which == "calibrate_camera":
            return self._calibrate_camera(cmd.calibrate_camera)
        if which == "stop":
            return self._stop(cmd.stop.keep_enabled)
        if which == "jog_axis":
            return self._jog_axis(cmd.jog_axis)
        target, kind, plan, disable_after = getattr(self, f"_plan_{which}")(getattr(cmd, which), cmd.allow_clock_error)
        self._start_track(target, kind, plan, disable_after)
        return mpb.TargetReply(accepted=True, message=f"{plan.mode} plan for {target.name}", plan=plan_summary(plan))

    def _near_az(self) -> float | None:
        try:
            return self.client.axis("az").cmd_position_rad
        except MountError:
            return None

    def _plan_track_radec(self, r, _allow_clock):
        return self._plan_fixed_sky(self._sidereal(r), r.duration_s, "radec")

    def _plan_track_body(self, b, _allow_clock):
        try:
            target = skyframe.BodyTarget(self.settings.site, b.body.strip().lower(), b.name)
        except ValueError as e:
            raise Rejected(str(e)) from None
        return self._plan_fixed_sky(target, b.duration_s, "body")

    def _sidereal(self, r) -> SiderealTarget:
        site = self.settings.site
        dec = r.dec_deg * DEG
        return SiderealTarget(
            site,
            r.ra_deg * DEG,
            dec,
            pmra=(r.pm_ra_mas_yr * MAS) / max(math.cos(dec), 1e-9),
            pmdec=r.pm_dec_mas_yr * MAS,
            parallax_as=r.parallax_mas / 1000.0,
            rv_km_s=r.radial_velocity_km_s,
            name=r.name or f"RA {r.ra_deg:.4f} Dec {r.dec_deg:+.4f}",
        )

    def _plan_fixed_sky(self, target, duration_s: float, kind: str):
        """A star or a solar-system body: tracked from now while it is above the horizon mask
        (at most duration_s, at most 8 h)."""
        tr = self.settings.tracking
        t0 = time.time_ns() + 2 * NS
        up = planner.find_passes(target, t0, t0 + 24 * 3600 * NS, tr.horizon_min_el_deg * DEG, step_s=60)
        if not up or up[0].rise_ns > t0 + 60 * NS:
            raise Rejected(f"{target.name} is below the {tr.horizon_min_el_deg:.0f} deg horizon")
        end = up[0].set_ns - 30 * NS
        if duration_s > 0:
            end = min(end, t0 + int(duration_s * NS))
        end = min(end, t0 + 8 * 3600 * NS)
        target = Sampled(target, t0 - 60 * NS, end + 60 * NS)  # exact ERFA every 60 s, cubic between
        best, plans = self._make_plan(target, t0, end, ("normal", "flipped"), step_s=1.0)
        if best is None:
            raise Rejected(_rejection(plans))
        return target, kind, best, True

    def _plan_track_tle(self, t, allow_clock_error: bool):
        site, tr = self.settings.site, self.settings.tracking
        self._clock_gate(allow_clock_error)
        try:
            target = SatelliteTarget(
                site,
                t.line1,
                t.line2,
                name=t.name or f"NORAD {t.line1[2:7].strip()}",
                time_offset_s=self._offsets.time_offset_s,
                cross_track_rad=self._offsets.cross_track_deg * DEG,
            )
            now = time.time_ns()
            end = max(now + 24 * 3600 * NS, t.rise_ns + 3 * 3600 * NS)
            passes = planner.find_passes(target, now, end, tr.horizon_min_el_deg * DEG)
        except (ValueError, SgpError) as e:
            raise Rejected(f"bad TLE: {e}") from None
        if t.rise_ns:
            # The pass that rises at rise_ns (±2 min), or contains it if it is already up.
            p = next((q for q in passes if q.set_ns > t.rise_ns - 120 * NS and q.rise_ns < t.rise_ns + 120 * NS), None)
            if p is None:
                raise Rejected(f"{target.name}: no pass rising near {_utc_hms(t.rise_ns)} UTC (over, or the TLE changed)")
        elif len(passes) <= t.pass_index:
            raise Rejected(f"{target.name}: no pass #{t.pass_index} in the next 24 h")
        else:
            p = passes[t.pass_index]
        if t.mode and t.mode not in ("normal", "flipped", "over_the_top"):
            raise Rejected(f"unknown mode '{t.mode}'")
        modes = (t.mode,) if t.mode else ("normal", "flipped", "over_the_top")
        best, plans = self._make_plan(target, max(p.rise_ns, now + 2 * NS), p.set_ns, modes)
        if best is None:
            raise Rejected(_rejection(plans))
        return target, "tle", best, True

    def _plan_track_aircraft(self, a, _allow_clock):
        """An aircraft from the local ADS-B receiver: planned along its current straight-line
        extrapolation while it is above the horizon mask (at most duration_s or
        adsb.max_track_s); a feeder thread then follows each new position (_aircraft_feed)."""
        tr, cfg = self.settings.tracking, self.settings.adsb
        ac = self._adsb.get(a.icao.strip())
        if ac is None:
            raise Rejected(f"aircraft {a.icao or '?'} not in the ADS-B data ({self._adsb.note})")
        age = (time.time_ns() - ac.t_pos_ns) / NS
        if age > cfg.max_age_s:
            raise Rejected(f"{ac.label}: last position {age:.0f} s old (limit {cfg.max_age_s:.0f} s)")
        target = adsb.AircraftTarget(self.settings.site, ac)
        t0 = time.time_ns() + 2 * NS
        span = min(a.duration_s or cfg.max_track_s, cfg.max_track_s)
        up = planner.find_passes(target, t0, t0 + int(span * NS), tr.horizon_min_el_deg * DEG, step_s=1.0)
        if not up or up[0].rise_ns > t0 + 30 * NS:
            raise Rejected(f"{ac.label} is below the {tr.horizon_min_el_deg:.0f} deg horizon"
                           + (f" (rises in {(up[0].rise_ns - t0) / NS:.0f} s)" if up else ""))
        end = up[0].set_ns - NS
        if end - max(t0, up[0].rise_ns) < 5 * NS:
            raise Rejected(f"{ac.label} is above the horizon for under 5 s")
        best, plans = self._make_plan(target, max(t0, up[0].rise_ns), end, ("normal", "flipped", "over_the_top"), step_s=0.5)
        if best is None:
            raise Rejected(_rejection(plans))
        return target, "aircraft", best, False  # hold at the end: the next aircraft is often close

    def _aircraft_feed(self, gen: int, target, control) -> None:
        """Follows a tracked aircraft: each new ADS-B position replaces the extrapolation and
        re-sends the buffered trajectory; a stale position holds the mount."""
        max_age = self.settings.adsb.max_age_s
        while not self._closing.wait(0.25):
            with self._lock:
                if gen != self._gen or self._control is not control:
                    return
            if control.stop_reason():
                return
            ac = self._adsb.get(target.aircraft.icao)
            if ac is not None and ac.t_pos_ns > target.aircraft.t_pos_ns:
                target.update(ac)
                control.request_resend()
                path = self._sky_path(target, self._plan)
                with self._lock:
                    if gen == self._gen:
                        self._path = path
            age = (time.time_ns() - target.aircraft.t_pos_ns) / NS
            if age > max_age:
                with self._lock:
                    self._stop_note[gen] = f": ADS-B position of {target.name} is {age:.0f} s old, holding"
                control.stop("hold")
                return

    def _plan_goto_altaz(self, a, _allow_clock):
        target = FixedTarget(a.az_deg * DEG, a.el_deg * DEG, name=a.name or f"az {a.az_deg:.2f} el {a.el_deg:.2f}")
        t0 = time.time_ns() + 2 * NS
        hold = a.hold_s if a.hold_s > 0 else 12 * 3600
        if a.mode not in ("", "normal", "flipped"):
            raise Rejected(f"alt-az mode must be normal or flipped, not '{a.mode}'")
        best, plans = self._make_plan(target, t0, t0 + int(hold * NS), (a.mode or "normal",), step_s=10.0)
        if best is None:
            raise Rejected(_rejection(plans))
        return target, "altaz", best, a.hold_s > 0

    def _make_plan(self, target, t0, t1, modes, step_s=0.1):
        s = self.settings
        plans = [
            planner.plan_mode(m, target, t0, t1, s.az, s.el, s.tracking, s.site, self.model, self._near_az(), step_s)
            for m in modes
        ]
        return planner.choose_plan(plans), plans

    def _clock_gate(self, allow: bool) -> None:
        if self.settings.backend == "sim" or allow:
            return
        st = clock.chrony_status()
        if st is None:
            raise Rejected("clock error unknown (chrony not available); set allow_clock_error to override")
        if st.error_bound_us > self.settings.max_clock_error_us:
            raise Rejected(
                f"clock error bound {st.error_bound_us:.0f} us > {self.settings.max_clock_error_us:.0f} us; "
                "set allow_clock_error to override"
            )

    def _start_track(self, target, kind, plan, disable_after) -> None:
        with self._lock:
            old_control, old_worker = self._control, self._worker
        if old_control:
            old_control.stop("retarget")  # no disable: the new track takes over the drives
        if old_worker:
            old_worker.join(timeout=10)
        d_az, d_el = self._offsets.az_arcsec * ARCSEC, self._offsets.el_arcsec * ARCSEC
        path = self._sky_path(target, plan)
        with self._lock:
            self._gen += 1
            gen = self._gen
            control = tracker.TrackControl(on_phase=lambda phase: self._on_phase(gen, phase))
            control.set_axis_offsets(d_az, d_el)
            self._target, self._target_kind, self._plan = target, kind, plan
            self._jog = None
            self._path = path
            self._state, self._message = "slewing", f"planned {plan.mode} for {target.name}"
            self._control = control
            self._worker = threading.Thread(
                target=self._run_worker, args=(gen, plan, control, disable_after), daemon=True, name=f"track-{gen}"
            )
            self._worker.start()
            if kind == "aircraft":
                threading.Thread(target=self._aircraft_feed, args=(gen, target, control), daemon=True,
                                 name=f"adsb-{gen}").start()

    def _on_phase(self, gen: int, phase: str) -> None:
        with self._lock:
            if gen == self._gen and phase in STATE:
                self._state = phase

    def _run_worker(self, gen, plan, control, disable_after) -> None:
        final, message = "idle", "finished"
        try:
            stats = tracker.track(self.client, plan, self.settings.tracking, disable_after, control=control)
            message = f"finished; max |cmd - act| {np.degrees(stats['max_cmd_act_rad']) * 3600:.1f} arcsec"
        except tracker.TrackStopped as e:
            message = f"stopped ({e.reason})" + self._stop_note.pop(gen, "")
            if e.reason == "retarget":
                return  # the next track reports from here on
        except (tracker.TrackAborted, MountError) as e:
            final, message = "error", f"aborted: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("track failed")
            final, message = "error", f"{type(e).__name__}: {e}"
        with self._lock:
            if gen == self._gen:
                self._state, self._message = final, message
                self._control = None

    def _set_offsets(self, o) -> mpb.TargetReply:
        with self._lock:
            self._offsets.CopyFrom(o)
            target, control = self._target, self._control
        if isinstance(target, SatelliteTarget):
            target.time_offset_s = o.time_offset_s
            target.cross_track_rad = o.cross_track_deg * DEG
        if control:
            control.set_axis_offsets(o.az_arcsec * ARCSEC, o.el_arcsec * ARCSEC)  # also re-sends
        return mpb.TargetReply(accepted=True, message="offsets set")

    def _add_offsets(self, d) -> None:
        with self._offsets_lock:
            with self._lock:
                o = mpb.Offsets()
                o.CopyFrom(self._offsets)
            o.time_offset_s += d.time_offset_s
            o.cross_track_deg += d.cross_track_deg
            o.az_arcsec += d.az_arcsec
            o.el_arcsec += d.el_arcsec
            self._set_offsets(o)

    # -- the camera loop ------------------------------------------------------------------------
    def _config_geometry(self) -> closedloop.CameraGeometry:
        cl = self.settings.closed_loop
        return closedloop.CameraGeometry.from_config(self.settings.loop_arcsec_per_px(), cl.rotation_deg, cl.flip)

    def _on_centroid(self, sample) -> None:
        c = mpb.CameraCentroid()
        try:
            c.ParseFromString(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        if c.state != "locked" or not (math.isfinite(c.dx) and math.isfinite(c.dy)):
            return
        b = max(int(c.bin), 1)
        m = closedloop.Measurement(int(c.t_ns), np.array([c.dx * b, c.dy * b]), float(c.snr))
        with self._loop_lock:
            self._last_centroid_ns = m.t_ns
            self._loop.add(m)
            self._recent.append(m)
            if len(self._recent) > 4000:
                del self._recent[:2000]

    def _closed_loop(self, on: bool) -> mpb.TargetReply:
        with self._loop_lock:
            if on and self._calibrating:
                raise Rejected("calibrating the camera; the loop starts again afterwards")
            st = self._loop.st
            st.on, st.note = on, ""
            st.state = "waiting" if on else "off"
            self._loop.reset(time.time_ns())
        cam = self.settings.closed_loop.camera
        return mpb.TargetReply(accepted=True, message=f"camera loop {'on' if on else 'off'} ({cam})")

    def _loop_target(self):
        """(plan, target, kind) while a track is in its tracking phase, else None."""
        with self._lock:
            if self._state != "tracking" or self._plan is None or self._control is None:
                return None
            return self._plan, self._target, self._target_kind

    def _loop_run(self) -> None:
        """The camera loop: about once a second, the averaged centroid becomes a correction."""
        while not self._closing.wait(0.05):
            with self._loop_lock:
                st = self._loop.st
                if not st.on or self._calibrating:
                    continue
            tgt = self._loop_target()
            now = time.time_ns()
            with self._loop_lock:
                if tgt is None:
                    st.state = "waiting"
                    self._loop.reset(now)
                    continue
                avg = self._loop.average(now)
                if avg is None:
                    fresh = now - self._last_centroid_ns < self._loop.s.max_age_s * NS
                    st.state = "settling" if fresh else "waiting"
                    continue
            plan, target, kind = tgt
            p, t = avg
            try:
                self._loop_correct(plan, target, kind, p, t)
            except Exception as e:  # noqa: BLE001 - a bad update opens the loop, never the track
                log.exception("camera loop update failed")
                with self._loop_lock:
                    st.on, st.state, st.note = False, "off", f"opened: {type(e).__name__}: {e}"

    def _loop_correct(self, plan, target, kind, p, t) -> None:
        with self._loop_lock:
            geom = self._geom
        q = geom.to_axis(*p)
        j, names, _e = closedloop.jacobian(plan, target, kind, t)
        x = closedloop.correction(j, names, q, self._loop.s.max_step_arcsec * ARCSEC)
        x = closedloop.clamp_px(x * self._loop.s.gain, j, geom, self._loop.s.max_step_px)
        err = float(np.hypot(*q))
        with self._loop_lock:
            st = self._loop.st
            st.err, st.err_px, st.t_err_ns = q, p, t
            if self._loop.diverging(err):
                st.on, st.state = False, "off"
                st.note = (f"opened: the error grew to {err / ARCSEC:.0f}\" over {st.updates} corrections; "
                           "check the camera geometry (Calibrate)")
                return
            st.moved = st.moved + j @ x
            if float(np.hypot(*st.moved)) > self._loop.s.max_total_arcsec * ARCSEC:
                st.on, st.state = False, "off"
                st.note = (f"opened: corrections added up to {np.hypot(*st.moved) / ARCSEC:.0f}\" without centring "
                           "the target; is the camera locked on the right object?")
                return
        d = mpb.Offsets()
        for name, v in zip(names, x):
            if name == "time_offset_s":
                d.time_offset_s = float(v)
            elif name == "cross_track_rad":
                d.cross_track_deg = math.degrees(v)
            elif name == "az_rad":
                d.az_arcsec = float(v) / ARCSEC
            elif name == "el_rad":
                d.el_arcsec = float(v) / ARCSEC
        log.debug("camera loop: centroid %s px, error %s arcsec, %s += %s", np.round(p, 1).tolist(),
                  np.round(q / ARCSEC, 1).tolist(), names, np.round(x, 7).tolist())
        self._add_offsets(d)
        label = {"time_offset_s": "time", "cross_track_rad": "cross-track", "az_rad": "az", "el_rad": "el"}
        with self._loop_lock:
            self._corrects = ", ".join(label[n] for n in names)
            self._loop.applied(time.time_ns())
            self._loop.st.state = "correcting"

    def _calibrate_camera(self, c) -> mpb.TargetReply:
        if c.reset:
            try:
                self._geom_path.unlink()
            except FileNotFoundError:
                pass
            with self._loop_lock:
                self._geom = self._config_geometry()
            return mpb.TargetReply(accepted=True, message="camera geometry: back to the system file's")
        if self._loop_target() is None:
            with self._lock:
                state, msg = self._state, self._message
            raise Rejected(f"calibrating needs the mount tracking a target; it is {state}" + (f" ({msg})" if msg else ""))
        with self._loop_lock:
            if self._calibrating:
                raise Rejected("already calibrating")
            if time.time_ns() - self._last_centroid_ns > NS:
                raise Rejected(f"no locked centroid from the {self.settings.closed_loop.camera} camera (ROI tracking on?)")
            self._calibrating = True
            self._loop.st.state = "calibrating"
        # Default: about max_step_px on the sensor, so the ROI tracker keeps the target.
        auto = self._loop.s.max_step_px * self._geom.describe()["arcsec_per_px"]
        step = (c.step_arcsec or self.settings.closed_loop.calibrate_step_arcsec or auto) * ARCSEC
        threading.Thread(target=self._calibrate_run, args=(step,), daemon=True, name="calibrate").start()
        return mpb.TargetReply(accepted=True, message=f"calibrating: {step / ARCSEC:.0f}\" moves in az and el")

    def _collect(self, after_ns: int, dur_s: float, timeout_s: float = 4.0) -> tuple[np.ndarray, float]:
        """Median locked centroid (unbinned px) and mean time over dur_s of centroids after after_ns."""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end and not self._closing.is_set():
            with self._loop_lock:
                ms = [m for m in self._recent if m.t_ns >= after_ns]
            if ms and ms[-1].t_ns - ms[0].t_ns >= dur_s * NS and len(ms) >= 3:
                return np.median([m.p for m in ms], axis=0), float(np.mean([m.t_ns for m in ms]))
            time.sleep(0.05)
        raise Rejected("the camera lost the target")

    def _calibrate_run(self, step: float) -> None:
        settle = int(self.settings.closed_loop.settle_s * NS) + NS // 2
        with self._lock:
            orig = mpb.Offsets()
            orig.CopyFrom(self._offsets)
        note = ""
        try:
            tgt = self._loop_target()
            if tgt is None:
                raise Rejected("the track ended")
            plan = tgt[0]

            def cos_e(t):
                return math.cos(float(plan.mech(np.int64(int(t)))[1][0]))

            if abs(cos_e(time.time_ns())) < 0.3:
                raise Rejected("too close to the zenith: the azimuth axis hardly moves the view there")
            p0, t0 = self._collect(time.time_ns(), 1.0)
            da = step / cos_e(time.time_ns() + settle)
            self._add_offsets(mpb.Offsets(az_arcsec=da / ARCSEC))
            p1, t1 = self._collect(time.time_ns() + settle, 1.0)
            self._add_offsets(mpb.Offsets(az_arcsec=-da / ARCSEC, el_arcsec=step / ARCSEC))
            p2, t2 = self._collect(time.time_ns() + settle, 1.0)
            self._add_offsets(mpb.Offsets(el_arcsec=-step / ARCSEC))
            p3, t3 = self._collect(time.time_ns() + settle, 1.0)
            log.info("calibration centroids (px): %s", [np.round(v, 2).tolist() for v in (p0, p1, p2, p3)])
            q1 = np.array([da * cos_e(t1), 0.0])
            q2 = np.array([0.0, step])
            stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            geom = closedloop.calibrate(p0, t0, p1, t1, p2, t2, p3, t3, q1, q2, f"calibrated {stamp}")
            g, ref = geom.describe(), self._config_geometry().describe()
            if not 0.25 < g["arcsec_per_px"] / ref["arcsec_per_px"] < 4:
                raise Rejected(f"measured {g['arcsec_per_px']:.3f}\"/px, far from the expected "
                               f"{ref['arcsec_per_px']:.3f}\"/px: the target moved for another reason?")
            geom.save(self._geom_path)
            with self._loop_lock:
                self._geom = geom
            note = (f"calibrated: {g['arcsec_per_px']:.3f}\"/px, sensor x at {g['rotation_deg']:.1f} deg from +az"
                    + (", mirrored" if g["flip"] else ""))
        except (Rejected, ValueError, MountError) as e:
            note = f"calibration failed: {e}"
            with self._offsets_lock:
                self._set_offsets(orig)
        except Exception as e:  # noqa: BLE001
            log.exception("calibration failed")
            note = f"calibration failed: {type(e).__name__}: {e}"
            with self._offsets_lock:
                self._set_offsets(orig)
        log.info("%s", note)
        with self._loop_lock:
            self._calibrating = False
            st = self._loop.st
            st.note = note
            st.state = "waiting" if st.on else "off"
            self._loop.reset(time.time_ns())

    def _loop_status(self) -> mpb.LoopStatus:
        now = time.time_ns()
        with self._loop_lock:
            st, g = self._loop.st, self._geom.describe()
            out = mpb.LoopStatus(
                on=st.on, state="calibrating" if self._calibrating else st.state, note=st.note,
                camera=self.settings.closed_loop.camera, updates=st.updates,
                err_x_arcsec=float(st.err[0]) / ARCSEC, err_y_arcsec=float(st.err[1]) / ARCSEC,
                err_arcsec=float(np.hypot(*st.err)) / ARCSEC, err_t_ns=st.t_err_ns, corrects=self._corrects,
                arcsec_per_px=g["arcsec_per_px"], rotation_deg=g["rotation_deg"], flip=g["flip"], geometry=g["source"],
                centroid_age_s=(now - self._last_centroid_ns) / NS if self._last_centroid_ns else -1.0,
                m_arcsec_per_px=[float(v) / ARCSEC for v in self._geom.m.flatten()])
        return out

    def _stop(self, keep_enabled: bool) -> mpb.TargetReply:
        with self._lock:
            control, worker = self._control, self._worker
        if control:
            control.stop("hold" if keep_enabled else "stop")
            if worker:
                worker.join(timeout=10)
        else:
            try:
                if keep_enabled:
                    self.client.clear_trajectory()  # e.g. a jog: decelerate and hold
                else:
                    self.client.disable()
            except MountError as e:
                raise Rejected(str(e)) from None
        with self._lock:
            self._state, self._message = "idle", "stopped (holding)" if keep_enabled else "stopped"
            self._target, self._plan, self._control = None, None, None
            self._target_kind, self._jog = "", None
            self._path = []
        return mpb.TargetReply(accepted=True, message="stopped")

    # -- commissioning -----------------------------------------------------------------------
    def _jog_axis(self, j) -> mpb.TargetReply:
        """A rest-to-rest move of one axis, streamed in one go; the axis then holds."""
        with self._lock:
            if self._control is not None:
                raise Rejected("a target is active: stop or hold it first")
            if self._jog is not None and time.time_ns() < self._jog["end_ns"]:
                raise Rejected(f"{self._jog['axis']} is still moving")
        lim = {"az": self.settings.az, "el": self.settings.el}.get(j.axis)
        if lim is None:
            raise Rejected(f"unknown axis '{j.axis}'")
        if not j.absolute and j.by_deg == 0:
            raise Rejected("jog by 0 deg")
        vel, acc = (j.vel_deg_s or 1.0) * DEG, (j.acc_deg_s2 or 2.0) * DEG
        if not (vel > 0 and acc > 0):
            raise Rejected("speed and acceleration must be > 0")
        vel, acc = min(vel, lim.max_vel), min(acc, lim.max_acc)
        try:
            a = self.client.axis(j.axis)
            if a.state != mpb.DRIVE_STATE_OPERATION_ENABLED:
                ok, msg = self.client.enable(j.axis)
                if not ok:
                    raise Rejected(f"enable {j.axis} refused: {msg}")
                deadline = time.monotonic() + 5.0
                while a.state != mpb.DRIVE_STATE_OPERATION_ENABLED:
                    if time.monotonic() > deadline:
                        raise Rejected(f"{j.axis} did not enable ({mpb.DriveState.Name(a.state)})")
                    self.client.telemetry(fresh=True)
                    a = self.client.axis(j.axis)
            start = a.cmd_position_rad
            target = j.to_deg * DEG if j.absolute else start + j.by_deg * DEG
            if not lim.min_rad <= target <= lim.max_rad:
                raise Rejected(
                    f"{j.axis} target {np.degrees(target):.4f} deg is outside the soft limits "
                    f"[{np.degrees(lim.min_rad):.1f}, {np.degrees(lim.max_rad):.1f}] deg"
                )
            move = tracker.MoveProfile(start, target, vel, acc)
            step = int(NS / self.settings.tracking.point_rate_hz)
            t0 = time.time_ns() + 300_000_000
            n = max(1, math.ceil(move.duration * NS / step))
            pts = [(t0 + k * step, *move.at(k * step / NS)) for k in range(n + 1)]
            pts.append((pts[-1][0] + NS // 2, target, 0.0))  # settle point
            self.client.clear_trajectory(j.axis)
            self.client.send_points({j.axis: pts})
        except MountError as e:
            raise Rejected(str(e)) from None
        name = f"jog {j.axis} to {np.degrees(target):.4f}°"
        msg = (f"jog {j.axis} {np.degrees(start):.4f} -> {np.degrees(target):.4f} deg "
               f"({np.degrees(target - start):+.4f}), {move.duration:.1f} s")
        with self._lock:
            self._gen += 1
            self._state, self._message = "slewing", msg
            self._target, self._target_kind, self._plan, self._path = None, "jog", None, []
            self._jog = {"axis": j.axis, "name": name, "end_ns": pts[-1][0]}
        return mpb.TargetReply(accepted=True, message=msg)

    def _jog_finished(self, now: int) -> None:
        with self._lock:
            jog = self._jog
        if jog is None or now < jog["end_ns"]:
            return
        try:
            a = self.client.axis(jog["axis"])
            done = f"jog done: {jog['axis']} at {np.degrees(a.act_position_rad):.4f} deg (holding)"
        except MountError as e:
            done = f"jog done ({e})"
        with self._lock:
            if self._jog is jog:
                self._jog, self._target_kind = None, ""
                self._state, self._message = "idle", done

    # -- passes ------------------------------------------------------------------------------
    def passes(self, req) -> mpb.PassesReply:
        s = self.settings
        try:
            target = SatelliteTarget(s.site, req.tle.line1, req.tle.line2, name=req.tle.name or "satellite")
        except ValueError as e:
            return mpb.PassesReply(ok=False, message=f"bad TLE: {e}")
        t0 = req.start_ns or time.time_ns()
        hours = req.hours if req.hours > 0 else 24.0
        reply = mpb.PassesReply(ok=True)
        try:
            found = planner.find_passes(target, t0, t0 + int(hours * 3600 * NS), s.tracking.horizon_min_el_deg * DEG)
        except SgpError as e:
            return mpb.PassesReply(ok=False, message=str(e))
        for p in found:
            best, plans = planner.plan_all(target, p.rise_ns, p.set_ns, s.az, s.el, s.tracking, s.site, self.model)
            info = reply.passes.add()
            info.rise_ns, info.culmination_ns, info.set_ns = p.rise_ns, p.culmination_ns, p.set_ns
            info.max_el_deg = float(np.degrees(p.max_el))
            info.culmination_az_deg = float(np.degrees(p.culmination_az))
            if best is not None:
                info.chosen.CopyFrom(plan_summary(best))
            for pl in plans:
                info.plans.add().CopyFrom(plan_summary(pl))
        reply.message = f"{len(found)} pass(es) in {hours:g} h"
        return reply

    # -- status ------------------------------------------------------------------------------
    def status(self) -> mpb.ServiceStatus:
        now = time.time_ns()
        if time.monotonic() - self._clock_checked > 10:
            self._clock_checked = time.monotonic()
            self._clock = clock.chrony_status()
        site = self.settings.site
        self._jog_finished(now)
        with self._lock:
            st = mpb.ServiceStatus(
                t_ns=now,
                state=STATE[self._state],
                target_name=getattr(self._target, "name", "") or (self._jog["name"] if self._jog else ""),
                target_kind=self._target_kind,
                message=self._message,
                clock_error_us=self._clock.error_bound_us if self._clock else -1.0,
                clock_source=self._clock.source if self._clock else "",
                site_source=site.position_source,
                site_lat_deg=site.latitude_deg,
                site_lon_deg=site.longitude_deg,
                site_height_m=site.height_m,
            )
            st.offsets.CopyFrom(self._offsets)
            st.plan.CopyFrom(plan_summary(self._plan))
        st.loop.CopyFrom(self._loop_status())
        with self._lock:
            target = self._target
            path = self._path
        st.path.extend(path)
        if target is not None:
            try:
                t2 = np.array([now, now + 500_000_000], dtype=np.int64)
                az, el = target.observed(t2)
                st.target_az_deg, st.target_el_deg = float(np.degrees(az[0])), float(np.degrees(el[0]))
                daz = (az[1] - az[0] + np.pi) % (2 * np.pi) - np.pi
                st.target_az_rate_deg_s = float(np.degrees(daz) / 0.5)
                st.target_el_rate_deg_s = float(np.degrees(el[1] - el[0]) / 0.5)
                if isinstance(target, SatelliteTarget):
                    g = target.geometry(now)
                    st.range_km, st.range_rate_km_s, st.altitude_km = g["range_km"], g["range_rate_km_s"], g["altitude_km"]
                    st.sunlit = bool(target.sunlit(np.int64(now))[0])
                    st.tle_line1, st.tle_line2 = target.line1, target.line2
                elif isinstance(target, adsb.AircraftTarget):
                    enu = target.enu_m(t2)
                    rng = np.linalg.norm(enu, axis=-1) / 1000
                    st.range_km, st.range_rate_km_s = float(rng[0]), float((rng[1] - rng[0]) / 0.5)
                    st.altitude_km = target.aircraft.alt_m / 1000
                    st.aircraft_icao = target.aircraft.icao
            except Exception:  # noqa: BLE001 - status must not fail
                pass
        return st

    def _sky_path(self, target, plan) -> list:
        """The plan's sky track, about 60 points, for the GUI's sky view."""
        try:
            t = np.linspace(plan.start_ns, plan.end_ns, min(120, max(2, (plan.end_ns - plan.start_ns) // (5 * NS)))).astype(np.int64)
            az, el = target.observed(t)
        except Exception:  # noqa: BLE001
            return []
        return [mpb.SkyPoint(t_ns=int(ti), az_deg=float(np.degrees(a)), el_deg=float(np.degrees(e))) for ti, a, e in zip(t, az, el)]

    def _status_loop(self) -> None:
        while not self._closing.wait(0.5):
            try:
                self._status_pub.put(self.status().SerializeToString())
            except Exception:  # noqa: BLE001
                log.exception("status publish failed")
