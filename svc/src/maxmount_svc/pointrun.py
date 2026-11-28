# SPDX-License-Identifier: GPL-3.0-or-later
"""Pointing model runs, inside mount-svc (svc/pointing; messages in pointing.proto).

A run (docs/design.md, "Automated pointing model"):
  1. plans points spread evenly over the sky between the horizon mask (+5 deg) and the zenith
     keyhole (80 deg), away from the Sun and the Moon; with `flipped`, every fourth point is
     taken past the zenith (elevation axis > 90 deg) so flip-side terms can be fitted; ordered
     greedily by slew time from where the mount is;
  2. for each point: goes to it (an alt-az target, through the same request queue as the GUI),
     waits for the tracking phase and settle_s, asks the widefield camera service to solve the
     next frame (camera/<camera>/cmd Solve; the result arrives in its status), and records the
     encoder angles at mid-exposure and the solved position;
  3. reduces and fits (pointfit.py) and keeps the fit until it is applied (saved as the active
     model, with history; mount-svc uses it from the next target) or discarded.

The run zeroes the az/el offsets and opens the camera loop first: the fit measures encoder
angles, so offsets would end up in IA/IE. Each point is a 5-minute alt-az hold: after the
run (or an abort) the mount holds the last point that long, then disables the drives.
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from concurrent.futures import Future

import erfa
import numpy as np

from . import astro, kinematics, pointfit
from .pointing import TERMS, PointingModel
from .proto import mpb

log = logging.getLogger(__name__)
DEG = math.pi / 180
NS = 1_000_000_000
GOLDEN = math.pi * (3 - math.sqrt(5))


def moon_observed(t_ns, site):
    """Observed az/el of the Moon (geocentric direction, about 1 deg off: fine for avoidance)."""
    t1, t2 = astro.tt_jd(np.int64(t_ns))
    pv = erfa.moon98(t1, t2)
    ra, dec = erfa.c2s(pv["p"])
    return astro.observed_from_icrs(np.int64(t_ns), site, ra, dec)


def plan_points(n: int, min_el: float, max_el: float, t_ns: int, site=None, avoid=(), flipped: bool = False,
                start: tuple[float, float] | None = None, rates=(1.0, 1.0), phase: float = 0.3) -> list[dict]:
    """n points (az, el in rad, flipped) evenly over the band of sky (a Fibonacci lattice in
    sin el), away from each (az, el, min_sep) in avoid, ordered by slew time from start
    (mechanical A, E)."""
    pts = []
    z0, z1 = math.sin(min_el), math.sin(max_el)
    for k in range(n):
        el = math.asin(z0 + (z1 - z0) * (k + 0.5) / n)
        az = (phase + k * GOLDEN) % (2 * math.pi)
        for _ in range(12):
            if all(astro.separation(az, el, a2, e2) >= sep for a2, e2, sep in avoid):
                break
            az = (az + 30 * DEG) % (2 * math.pi)
        else:
            continue
        pts.append({"az": az, "el": el, "flipped": flipped and k % 4 == 2})
    # Greedy order by slew time (mechanical angles; azimuth difference wrapped).
    pos = start
    out = []
    left = list(pts)
    while left:
        def cost(p):
            a, e = kinematics.to_mech(p["az"], p["el"], p["flipped"])
            if pos is None:
                return float(a)
            da = abs((float(a) - pos[0] + math.pi) % (2 * math.pi) - math.pi)
            return max(da / rates[0], abs(float(e) - pos[1]) / rates[1])
        p = min(left, key=cost)
        left.remove(p)
        out.append(p)
        a, e = kinematics.to_mech(p["az"], p["el"], p["flipped"])
        pos = (float(a), float(e))
    return out


class PointingRun:
    def __init__(self, svc):
        self.svc = svc
        self.settings = svc.settings
        self.cfg = svc.settings.pointing_run
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._state, self._message, self._mode = "idle", "no run yet", ""
        self._points: list[dict] = []
        self._fit: pointfit.Fit | None = None
        self._applied = False
        self._abort = threading.Event()
        self._thread: threading.Thread | None = None
        self._solutions: dict[str, mpb.PlateSolution] = {}
        self._sub = None

    def start_listening(self) -> None:
        self._sub = self.svc.client.session.declare_subscriber(
            self.svc.client.key(f"camera/{self.cfg.camera}/status"), self._on_camera)
        self.svc._decls.append(self._sub)

    def _on_camera(self, sample) -> None:
        st = mpb.CameraStatus()
        try:
            st.ParseFromString(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        sid = st.solution.id
        if sid:
            with self._cv:
                if self._solutions.get(sid, 0) is None:  # one this run asked for
                    self._solutions[sid] = st.solution
                    self._cv.notify_all()

    # -- requests -----------------------------------------------------------------------------
    def handle(self, req) -> mpb.PointingReply:
        op = req.WhichOneof("op")
        ok, msg = True, ""
        if op == "start":
            ok, msg = self.start(req.start)
        elif op == "abort":
            self._abort.set()
            msg = "aborting the run" if self.running else "no run in progress"
        elif op == "apply":
            ok, msg = self.apply()
        elif op == "discard":
            with self._lock:
                had, self._fit = self._fit is not None, None
                if had:
                    self._state, self._message = "idle", "fit discarded"
            msg = "fit discarded" if had else "no fit to discard"
        else:
            msg = self._message
        return mpb.PointingReply(ok=ok, message=msg, status=self.status())

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, r) -> tuple[bool, str]:
        if self.running:
            return False, "a run is already in progress"
        mode = r.mode or "site"
        if mode not in ("site", "full"):
            return False, f"mode must be site or full, not '{mode}'"
        n = r.points or (10 if mode == "site" else 40)
        if not 4 <= n <= 120:
            return False, "points: 4..120"
        s, tr = self.settings, self.settings.tracking
        now = time.time_ns()
        min_el = (self.cfg.min_el_deg or tr.horizon_min_el_deg + 5) * DEG
        max_el = self.cfg.max_el_deg * DEG
        avoid = []
        try:
            saz, sel = astro.sun_observed(np.int64(now), s.site)
            avoid.append((float(saz), float(sel), (self.cfg.sun_min_sep_deg or tr.sun_min_sep_deg) * DEG))
            maz, mel = moon_observed(now, s.site)
            if float(mel) > -2 * DEG:
                avoid.append((float(maz), float(mel), self.cfg.moon_min_sep_deg * DEG))
        except Exception:  # noqa: BLE001 - avoidance is a nicety
            log.exception("sun/moon for pointing points")
        flipped = (r.flipped or mode == "full") and s.el.max_rad > 135 * DEG
        try:
            tel = self.svc.client.telemetry(timeout=1.0)
            ax = {a.axis: a for a in tel.axes}
            start = (ax["az"].act_position_rad, ax["el"].act_position_rad)
        except Exception:  # noqa: BLE001
            start = None
        rates = (min(tr.slew_vel_deg_s, math.degrees(s.az.max_vel)), min(tr.slew_vel_deg_s, math.degrees(s.el.max_vel)))
        pts = plan_points(n, min_el, max_el, now, s.site, avoid, flipped, start, rates, phase=(now / NS / 977) % (2 * math.pi))
        if len(pts) < 4:
            return False, "too few points away from the Sun and the Moon"
        with self._lock:
            self._points = [{"index": i, "az": p["az"], "el": p["el"], "flipped": p["flipped"], "state": "pending",
                             "message": ""} for i, p in enumerate(pts)]
            self._fit, self._applied, self._mode = None, False, mode
            self._state, self._message = "running", f"{mode} run: {len(pts)} points"
        self._abort.clear()
        settle = r.settle_s if r.settle_s > 0 else self.cfg.settle_s
        self._thread = threading.Thread(target=self._run, args=(settle,), daemon=True, name="pointing-run")
        self._thread.start()
        return True, f"pointing run started: {len(pts)} points ({mode})"

    def apply(self) -> tuple[bool, str]:
        with self._lock:
            f, n = self._fit, sum(1 for p in self._points if p["state"] == "solved")
        if f is None:
            return False, "no fit to apply"
        doc = pointfit.save_model(self.settings, f, n)
        self.svc.model = PointingModel({k: v for k, v in f.terms.items()})
        with self._lock:
            self._applied = True
            self._message = f"applied model {doc['version']} (rms {f.rms_after:.1f}\"); used from the next target"
        return True, self._message

    # -- the run ------------------------------------------------------------------------------
    def _request(self, cmd, timeout: float = 30.0) -> mpb.TargetReply:
        fut: Future = Future()
        self.svc._requests.put((cmd, fut))
        return fut.result(timeout=timeout)

    def _set_point(self, i: int, **kw) -> None:
        with self._lock:
            self._points[i].update(kw)

    def _run(self, settle_s: float) -> None:
        try:
            self._request(mpb.TargetCommand(set_offsets=mpb.Offsets()))
            self._request(mpb.TargetCommand(closed_loop=mpb.ClosedLoop(on=False)))
            n = len(self._points)
            for i in range(n):
                if self._abort.is_set():
                    break
                self._point(i, n, settle_s)
            with self._lock:
                aborted = self._abort.is_set()
            self._finish(aborted)
        except Exception as e:  # noqa: BLE001
            log.exception("pointing run failed")
            with self._lock:
                self._state, self._message = "error", f"{type(e).__name__}: {e}"

    def _wait_tracking(self, gen: int, timeout_s: float) -> str:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end and not self._abort.is_set():
            with self.svc._lock:
                g, state, msg = self.svc._gen, self.svc._state, self.svc._message
            if g != gen:
                return "another target took over"
            if state == "tracking":
                return ""
            if state in ("error", "idle"):
                return f"mount {state}: {msg}"
            time.sleep(0.1)
        return "aborted" if self._abort.is_set() else "timed out reaching the point"

    def _point(self, i: int, n: int, settle_s: float) -> None:
        p = self._points[i]
        self._set_point(i, state="slewing")
        with self._lock:
            self._message = f"point {i + 1}/{n}: slewing"
        # A short hold: a long one is planned (Sun, limits) for its whole length; the next point
        # takes over long before it ends, and an abandoned run disables the drives after it.
        cmd = mpb.TargetCommand(goto_altaz=mpb.AltAz(az_deg=math.degrees(p["az"]), el_deg=math.degrees(p["el"]), hold_s=300,
                                                     name=f"pointing {i + 1}/{n}", mode="flipped" if p["flipped"] else ""))
        r = self._request(cmd)
        if not r.accepted:
            self._set_point(i, state="failed", message=r.message)
            return
        with self.svc._lock:
            gen = self.svc._gen
        why = self._wait_tracking(gen, 180)
        if why:
            self._set_point(i, state="failed", message=why)
            return
        self._set_point(i, state="settling")
        if self._abort.wait(settle_s):
            self._set_point(i, state="failed", message="aborted")
            return
        self._set_point(i, state="solving")
        with self._lock:
            self._message = f"point {i + 1}/{n}: solving"
        sol = self._solve()
        if isinstance(sol, str):
            self._set_point(i, state="failed", message=sol)
            return
        if not sol.ok or not sol.has_mount:
            self._set_point(i, state="failed", message=sol.message, matched=sol.matched)
            return
        a_enc, e_enc = sol.enc_az_deg * DEG, sol.enc_el_deg * DEG
        a_i, e_i, az, el = pointfit.reduce(self.settings.site, sol.t_ns, sol.ra_deg * DEG, sol.dec_deg * DEG, a_enc, e_enc)
        self._set_point(i, state="solved", message=sol.message, t_ns=sol.t_ns, enc_a=a_enc, enc_e=e_enc, a_i=a_i, e_i=e_i,
                        ra=sol.ra_deg, dec=sol.dec_deg, obs_az=az, obs_el=el, matched=sol.matched, solve_rms=sol.rms_arcsec,
                        before=[sol.err_xel_arcsec, sol.err_el_arcsec])  # the fit recomputes it (the same)

    def _solve(self):
        sid = uuid.uuid4().hex[:12]
        cmd = mpb.CameraCommand(solve=mpb.Solve(id=sid))
        with self._cv:
            self._solutions[sid] = None  # wanted
        try:
            out = self.svc.client.query(f"camera/{self.cfg.camera}/cmd", cmd.SerializeToString(), timeout=5, discovery_s=3)
            if out is None:
                return f"no reply from the {self.cfg.camera} camera service: is 'mount-svc widefield' running?"
            rep = mpb.CameraReply()
            rep.ParseFromString(out)
            if not rep.ok:
                return rep.message
            end = time.monotonic() + self.cfg.solve_timeout_s
            with self._cv:
                while self._solutions.get(sid) is None:
                    left = end - time.monotonic()
                    if left <= 0 or self._abort.is_set():
                        return "aborted" if self._abort.is_set() else "the solve timed out"
                    self._cv.wait(min(left, 0.5))
                return self._solutions[sid]
        finally:
            with self._cv:
                self._solutions.pop(sid, None)

    def _finish(self, aborted: bool) -> None:
        with self._lock:
            pts = [p for p in self._points if p["state"] == "solved"]
            mode = self._mode
        need = 4 if mode == "site" else 6
        if len(pts) < need:
            with self._lock:
                self._state = "aborted" if aborted else "error"
                self._message = f"{'aborted' if aborted else 'finished'} with {len(pts)} solved point(s); {need} needed for a fit"
            return
        a_i = np.array([p["a_i"] for p in pts])
        e_i = np.array([p["e_i"] for p in pts])
        da = np.array([p["enc_a"] - p["a_i"] for p in pts])
        de = np.array([p["enc_e"] - p["e_i"] for p in pts])
        try:
            f = pointfit.fit(a_i, e_i, da, de, mode=mode, current=self.svc.model)
        except (ValueError, np.linalg.LinAlgError) as e:
            with self._lock:
                self._state, self._message = "error", f"fit failed: {e}"
            return
        for k, p in enumerate(pts):
            p.update(before=f.res_before[k].tolist(), after=f.res_after[k].tolist(), rejected=not bool(f.used[k]))
        with self._lock:
            self._fit = f
            self._state = "aborted" if aborted else "done"
            self._message = (f"{'aborted; ' if aborted else ''}fit of {int(f.used.sum())} points: rms {f.rms_before:.0f}\" "
                             f"-> {f.rms_after:.1f}\"; Apply to use it")

    # -- status -------------------------------------------------------------------------------
    def _info(self, doc: dict, active: bool = False) -> mpb.PointingModelInfo:
        info = mpb.PointingModelInfo(version=str(doc.get("version", "")), date=str(doc.get("date", "")),
                                     mode=str(doc.get("mode", "")), points=int(doc.get("points", 0) or 0),
                                     rms_arcsec=float(doc.get("rms_arcsec", 0) or 0),
                                     rms_before_arcsec=float(doc.get("rms_before_arcsec", 0) or 0), active=active)
        sig = doc.get("sigma") or {}
        for t in TERMS:
            v = (doc.get("terms") or {}).get(t)
            if v is not None:
                info.terms.add(name=t, value_arcsec=float(v), sigma_arcsec=float(sig.get(t, 0) or 0), fitted=t in sig)
        return info

    def status(self) -> mpb.PointingStatus:
        st = mpb.PointingStatus(t_ns=time.time_ns())
        with self._lock:
            st.state, st.message, st.mode, st.applied = self._state, self._message, self._mode, self._applied
            for p in self._points:
                q = st.points.add(index=p["index"], az_deg=math.degrees(p["az"]), el_deg=math.degrees(p["el"]),
                                  flipped=p["flipped"], state=p["state"], message=p.get("message", ""))
                if p["state"] == "solved":
                    q.t_ns = p["t_ns"]
                    q.enc_az_deg, q.enc_el_deg = math.degrees(p["enc_a"]), math.degrees(p["enc_e"])
                    q.ra_deg, q.dec_deg = p["ra"], p["dec"]
                    q.obs_az_deg, q.obs_el_deg = math.degrees(p["obs_az"]), math.degrees(p["obs_el"])
                    q.matched, q.solve_rms_arcsec = int(p.get("matched", 0)), float(p.get("solve_rms", 0))
                    if "before" in p:
                        q.before_xel_arcsec, q.before_el_arcsec = p["before"]
                    if "after" in p:
                        q.after_xel_arcsec, q.after_el_arcsec = p["after"]
                        q.rejected = p["rejected"]
            f = self._fit
        if f is not None:
            doc = {"mode": f.mode, "points": len(f.used), "rms_arcsec": f.rms_after, "rms_before_arcsec": f.rms_before,
                   "terms": {t: v for t, v in f.terms.items()}, "sigma": f.sigma}
            st.fit.CopyFrom(self._info(doc))
            st.notes.extend(f.notes)
        cur = pointfit.read_info(pointfit.model_path(self.settings)) if pointfit.model_path(self.settings).exists() else {}
        if not cur:
            cur = {"terms": dict(self.svc.model.coefficients), "version": "none" if not self.svc.model.coefficients else "system file"}
        info = self._info(cur, active=True)
        info.path = str(pointfit.model_path(self.settings))
        st.current.CopyFrom(info)
        for d in pointfit.history(self.settings, 10):
            st.history.add().CopyFrom(self._info(d, active=d.get("version") == cur.get("version")))
        return st
