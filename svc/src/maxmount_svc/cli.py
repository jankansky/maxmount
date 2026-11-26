# SPDX-License-Identifier: GPL-3.0-or-later
"""mount-svc command line.

    mount-svc --system FILE passes   --tle FILE [--name NAME] [--hours H]
    mount-svc --system FILE track-tle --tle FILE [--name NAME] [--pass N] [--mode MODE]
    mount-svc --system FILE track-radec --ra HH:MM:SS --dec DD:MM:SS [--minutes M]
    mount-svc --system FILE goto --az DEG --el DEG
    mount-svc --system FILE serve                 run as a Zenoh service (GUI, scheduler)
    mount-svc --system FILE request ...           send a request to a running service
    mount-svc --system FILE gui                   serve the web GUI on port 8080

Tracking commands drive mount-rt over Zenoh; mount-rt must run with --serve.
"""
from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import clock, planner, tracker
from .config import ConfigError, SystemSettings, load_system
from .mount import MountClient, MountError
from .pointing import PointingModel
from .targets import FixedTarget, SatelliteTarget, SiderealTarget

NS = planner.NS
DEG = math.pi / 180


def _utc(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / NS, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def read_tle(path: str, name: str | None) -> tuple[str, str, str]:
    """First TLE in the file (2- or 3-line format) whose name contains `name`."""
    lines = [ln.rstrip() for ln in Path(path).read_text().splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            title, l1, l2 = f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1]
            i += 2
        elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            title, l1, l2 = lines[i].lstrip("0 ").strip(), lines[i + 1], lines[i + 2]
            i += 3
        else:
            i += 1
            continue
        if name is None or name.lower() in title.lower():
            return title, l1, l2
    raise SystemExit(f"no TLE{' matching ' + repr(name) if name else ''} in {path}")


def _sexagesimal(s: str, hours: bool) -> float:
    parts = [float(p) for p in s.replace(":", " ").split()]
    sign = -1.0 if s.strip().startswith("-") else 1.0
    value = sum(abs(p) / 60**i for i, p in enumerate(parts))
    return sign * value * (15.0 if hours else 1.0) * DEG


def _need_site(sys_: SystemSettings):
    if sys_.site is None:
        raise SystemExit("the system file has no site: section (latitude_deg, longitude_deg, height_m)")
    return sys_.site


def _clock_gate(sys_: SystemSettings, allow: bool) -> None:
    if sys_.backend == "sim":
        return
    st = clock.chrony_status()
    if st is None:
        msg = "chrony is not available, so the clock error is unknown"
    elif st.error_bound_us > sys_.max_clock_error_us:
        msg = f"clock error bound {st.error_bound_us:.0f} us > {sys_.max_clock_error_us:.0f} us (source {st.source})"
    else:
        return
    if not allow:
        raise SystemExit(f"refusing a satellite pass: {msg} (override with --allow-clock-error)")
    print(f"warning: {msg}", file=sys.stderr)


def _print_plans(plans, chosen):
    if plans and plans[0]._target is not None:
        az, el = plans[0]._target.observed(plans[0].start_ns)
        print(f"  target at start: az {np.degrees(az):.3f} deg, el {np.degrees(el):.3f} deg (observed)")
    for p in plans:
        mark = "*" if p is chosen else " "
        err = f" max error {np.degrees(p.max_error):.2f} deg" if p.mode == "over_the_top" else ""
        status = "ok" if p.feasible else "; ".join(p.problems)
        print(
            f"  {mark} {p.mode:12s} peak az {np.degrees(p.peak_vel[0]):7.3f} deg/s {np.degrees(p.peak_acc[0]):7.3f} deg/s^2, "
            f"el {np.degrees(p.peak_vel[1]):6.3f} deg/s{err}: {status}"
        )


def _run(client: MountClient, plan, sys_: SystemSettings, disable_after: bool) -> int:
    interrupted = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: interrupted.__setitem__("flag", True))
    try:
        stats = tracker.track(client, plan, sys_.tracking, disable_after, stop_flag=lambda: interrupted["flag"])
    except tracker.TrackAborted as e:
        print(f"tracking aborted: {e}; drives disabled", file=sys.stderr)
        return 1
    except tracker.TrackStopped:
        print("stopped; drives disabled", file=sys.stderr)
        return 1
    n = max(stats["samples"], 1)
    print(
        f"done: max |cmd - plan| {np.degrees(stats['max_cmd_plan_rad']) * 3600:.1f} arcsec, "
        f"max |cmd - act| {np.degrees(stats['max_cmd_act_rad']) * 3600:.1f} arcsec, "
        f"limited in {100.0 * stats['limited_samples'] / n:.1f}% of telemetry samples"
    )
    return 0


def cmd_passes(args, sys_: SystemSettings) -> int:
    site = _need_site(sys_)
    title, l1, l2 = read_tle(args.tle, args.name)
    sat = SatelliteTarget(site, l1, l2, name=title)
    t0 = time.time_ns()
    passes = planner.find_passes(sat, t0, t0 + int(args.hours * 3600 * NS), math.radians(sys_.tracking.horizon_min_el_deg))
    model = PointingModel.load(sys_.pointing_file)
    print(f"{title}: {len(passes)} pass(es) above {sys_.tracking.horizon_min_el_deg:.0f} deg in {args.hours:g} h")
    for i, p in enumerate(passes):
        print(
            f"[{i}] {_utc(p.rise_ns)} UTC  {p.duration_s / 60:4.1f} min  max el {np.degrees(p.max_el):5.1f} deg "
            f"at az {np.degrees(p.culmination_az):5.1f}"
        )
        chosen, plans = planner.plan_all(sat, p.rise_ns, p.set_ns, sys_.az, sys_.el, sys_.tracking, site, model)
        _print_plans(plans, chosen)
    return 0


def cmd_track_tle(args, sys_: SystemSettings) -> int:
    site = _need_site(sys_)
    _clock_gate(sys_, args.allow_clock_error)
    title, l1, l2 = read_tle(args.tle, args.name)
    sat = SatelliteTarget(site, l1, l2, name=title, time_offset_s=args.time_offset, cross_track_rad=args.cross_track * DEG)
    t0 = time.time_ns()
    passes = planner.find_passes(sat, t0, t0 + 24 * 3600 * NS, math.radians(sys_.tracking.horizon_min_el_deg))
    if len(passes) <= args.pass_index:
        raise SystemExit(f"{title}: no pass #{args.pass_index} in the next 24 h")
    p = passes[args.pass_index]
    model = PointingModel.load(sys_.pointing_file)
    with MountClient(sys_.zenoh) as client:
        near = client.axis("az").cmd_position_rad
        modes = (args.mode,) if args.mode else None
        chosen, plans = planner.plan_all(sat, p.rise_ns, p.set_ns, sys_.az, sys_.el, sys_.tracking, site, model, near, modes)
        print(f"{title}: pass at {_utc(p.rise_ns)} UTC, max el {np.degrees(p.max_el):.1f} deg")
        _print_plans(plans, chosen)
        if chosen is None:
            print("no feasible plan", file=sys.stderr)
            return 1
        wait = (p.rise_ns - time.time_ns()) / NS - 60
        if wait > 0:
            print(f"waiting {wait / 60:.1f} min until 1 min before rise (Ctrl-C to cancel)")
            time.sleep(wait)
        return _run(client, chosen, sys_, disable_after=True)


def cmd_track_radec(args, sys_: SystemSettings) -> int:
    site = _need_site(sys_)
    target = SiderealTarget(site, _sexagesimal(args.ra, True), _sexagesimal(args.dec, False), name="ra/dec")
    model = PointingModel.load(sys_.pointing_file)
    t0 = time.time_ns() + 2 * NS
    with MountClient(sys_.zenoh) as client:
        near = client.axis("az").cmd_position_rad
        chosen, plans = planner.plan_all(
            target, t0, t0 + int(args.minutes * 60 * NS), sys_.az, sys_.el, sys_.tracking, site, model, near,
            ("normal", "flipped"),
        )
        _print_plans(plans, chosen)
        if chosen is None:
            print("no feasible plan (below the horizon, near the Sun or outside the limits for that long?)", file=sys.stderr)
            return 1
        return _run(client, chosen, sys_, disable_after=not args.hold)


def cmd_goto(args, sys_: SystemSettings) -> int:
    target = FixedTarget(args.az * DEG, args.el * DEG, name="fixed")
    model = PointingModel.load(sys_.pointing_file)
    t0 = time.time_ns() + 2 * NS
    with MountClient(sys_.zenoh) as client:
        near = client.axis("az").cmd_position_rad
        chosen, plans = planner.plan_all(
            target, t0, t0 + int(args.hold_s * NS), sys_.az, sys_.el, sys_.tracking, sys_.site, model, near, ("normal",)
        )
        _print_plans(plans, chosen)
        if chosen is None:
            return 1
        return _run(client, chosen, sys_, disable_after=not args.stay_enabled)


def cmd_serve(args, sys_: SystemSettings) -> int:
    from .service import MountService

    _need_site(sys_)
    with MountClient(sys_.zenoh) as client:
        svc = MountService(sys_, client)
        svc.start()
        print(f"mount-svc serving on {client.key('svc/*')} (site from {sys_.site.position_source}); Ctrl-C to stop")
        stop = {"flag": False}
        signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
        signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
        while not stop["flag"]:
            time.sleep(0.2)
        print("stopping: disabling the drives")
        svc.close(disable=True)
    return 0


def _service_query(client: MountClient, key: str, msg, reply_type, timeout: float = 30.0):
    out = client.query(key, msg.SerializeToString(), timeout=timeout)
    if out is None:
        raise MountError(f"no reply on {client.key(key)}: is 'mount-svc serve' running?")
    reply = reply_type()
    reply.ParseFromString(out)
    return reply


def cmd_request(args, sys_: SystemSettings) -> int:
    from .proto import mpb

    with MountClient(sys_.zenoh) as client:
        if args.what == "status":
            got = {}
            sub = client.session.declare_subscriber(client.key("svc/status"), lambda s: got.setdefault("s", s.payload.to_bytes()))
            deadline = time.monotonic() + 3
            while "s" not in got and time.monotonic() < deadline:
                time.sleep(0.05)
            sub.undeclare()
            if "s" not in got:
                print("no status: is 'mount-svc serve' running?", file=sys.stderr)
                return 1
            st = mpb.ServiceStatus()
            st.ParseFromString(got["s"])
            print(st)
            return 0
        if args.what == "passes":
            title, l1, l2 = read_tle(args.tle, args.name)
            req = mpb.PassesRequest(tle=mpb.Tle(name=title, line1=l1, line2=l2), hours=args.hours)
            rep = _service_query(client, "svc/passes", req, mpb.PassesReply)
            print(rep.message)
            for i, p in enumerate(rep.passes):
                chosen = p.chosen.mode or "none fits"
                print(f"[{i}] {_utc(p.rise_ns)} UTC  max el {p.max_el_deg:5.1f} deg  plan: {chosen}")
            return 0 if rep.ok else 1

        cmd = mpb.TargetCommand(allow_clock_error=getattr(args, "allow_clock_error", False))
        if args.what == "radec":
            cmd.track_radec.CopyFrom(
                mpb.RaDec(ra_deg=_sexagesimal(args.ra, True) / DEG, dec_deg=_sexagesimal(args.dec, False) / DEG, duration_s=args.minutes * 60)
            )
        elif args.what == "tle":
            title, l1, l2 = read_tle(args.tle, args.name)
            cmd.track_tle.CopyFrom(mpb.Tle(name=title, line1=l1, line2=l2, pass_index=args.pass_index, mode=args.mode or ""))
        elif args.what == "altaz":
            cmd.goto_altaz.CopyFrom(mpb.AltAz(az_deg=args.az, el_deg=args.el, hold_s=args.hold_s))
        elif args.what == "offsets":
            cmd.set_offsets.CopyFrom(
                mpb.Offsets(time_offset_s=args.time, cross_track_deg=args.cross, az_arcsec=args.az, el_arcsec=args.el)
            )
        elif args.what == "stop":
            cmd.stop.CopyFrom(mpb.Stop(keep_enabled=args.hold))
        rep = _service_query(client, "svc/target", cmd, mpb.TargetReply)
        print(f"{'accepted' if rep.accepted else 'rejected'}: {rep.message}")
        if rep.plan.mode:
            print(
                f"  plan {rep.plan.mode}: {_utc(rep.plan.start_ns)} - {_utc(rep.plan.end_ns)} UTC, "
                f"peak az {rep.plan.peak_az_deg_s:.3f} deg/s, el {rep.plan.peak_el_deg_s:.3f} deg/s"
            )
        return 0 if rep.accepted else 1


def cmd_gui(args, sys_: SystemSettings) -> int:
    import asyncio

    from .gateway import Gateway

    async def run():
        gw = Gateway(sys_, host=args.host, port=args.port, park_az_deg=args.park_az, park_el_deg=args.park_el)
        await gw.start()
        print(f"maxmount GUI on http://{args.host}:{args.port} (local network only: no login yet); Ctrl-C to stop")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await gw.stop()

    asyncio.run(run())
    return 0


def cmd_adsb_sim(args, sys_: SystemSettings) -> int:
    from . import adsb

    site = _need_site(sys_)
    out = args.out or sys_.adsb.source
    if out.startswith(("http://", "https://")):
        print("mount-svc: adsb.source is a URL; give --out PATH", file=sys.stderr)
        return 1
    print(f"writing {args.aircraft} simulated aircraft around the site to {out} once a second; Ctrl-C to stop")
    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    adsb.run_simulator(site, out, n=args.aircraft, seed=args.seed, stop=lambda: bool(stop))
    return 0


def cmd_dslr(args, sys_: SystemSettings) -> int:
    from .camera import CameraError, GphotoBackend, SimBackend

    cfg = sys_.dslr
    try:
        backend = SimBackend() if (args.sim or cfg.backend == "sim") else GphotoBackend()
    except CameraError as e:
        print(f"mount-svc: {e}", file=sys.stderr)
        return 1
    return _serve_camera(sys_, backend, "dslr", cfg.data_dir, cfg.liveview_fps,
                         cfg.save_to if cfg.save_to in ("card", "pi", "both") else "card")


def cmd_zwo(args, sys_: SystemSettings) -> int:
    from .camera import CameraError
    from .zwo import ZwoSdkBackend, ZwoSimBackend

    cfg = sys_.zwo
    try:
        backend = ZwoSimBackend() if (args.sim or cfg.backend == "sim") else ZwoSdkBackend(cfg.sdk_lib)
        backend.record_buffer_bytes = int(cfg.record_buffer_mb * 1e6)
        backend.record_min_free_bytes = int(cfg.record_min_free_mb * 1e6)
    except CameraError as e:
        print(f"mount-svc: {e}", file=sys.stderr)
        return 1
    return _serve_camera(sys_, backend, "zwo", cfg.data_dir, cfg.liveview_fps, "pi")


def _serve_camera(sys_: SystemSettings, backend, camera_id: str, data_dir: str, fps: float, save_to: str) -> int:
    from .camera import CameraService
    from .mount import MountClient

    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    with MountClient(sys_.zenoh) as client:
        svc = CameraService(client, backend, camera_id, data_dir, fps)
        if save_to in svc.storage:
            svc._save_to = save_to
        svc.start()
        print(f"camera service on {svc.key('*')} ({'simulated' if backend.simulated else camera_id}); Ctrl-C to stop")
        while not stop:
            time.sleep(0.2)
        svc.close()
        backend.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mount-svc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", required=True, help="config/systems/<name>/system.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("passes", help="list upcoming passes and their plans")
    p.add_argument("--tle", required=True)
    p.add_argument("--name")
    p.add_argument("--hours", type=float, default=24.0)
    p.set_defaults(func=cmd_passes)

    p = sub.add_parser("track-tle", help="track a satellite pass")
    p.add_argument("--tle", required=True)
    p.add_argument("--name")
    p.add_argument("--pass", dest="pass_index", type=int, default=0, help="index from 'passes' (default: next)")
    p.add_argument("--mode", choices=("normal", "flipped", "over_the_top"))
    p.add_argument("--time-offset", type=float, default=0.0, help="along-track correction, s")
    p.add_argument("--cross-track", type=float, default=0.0, help="cross-track correction, deg")
    p.add_argument("--allow-clock-error", action="store_true")
    p.set_defaults(func=cmd_track_tle)

    p = sub.add_parser("track-radec", help="track an ICRS position")
    p.add_argument("--ra", required=True, help="HH:MM:SS.s")
    p.add_argument("--dec", required=True, help="+DD:MM:SS")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--hold", action="store_true", help="stay enabled at the end")
    p.set_defaults(func=cmd_track_radec)

    p = sub.add_parser("goto", help="slew to a fixed az/el and hold")
    p.add_argument("--az", type=float, required=True)
    p.add_argument("--el", type=float, required=True)
    p.add_argument("--hold-s", type=float, default=5.0)
    p.add_argument("--stay-enabled", action="store_true")
    p.set_defaults(func=cmd_goto)

    p = sub.add_parser("serve", help="run as a Zenoh service for the GUI and scheduler")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("gui", help="serve the web GUI (bridges the browser to Zenoh)")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default all interfaces)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--park-az", type=float, default=0.0, help="park azimuth, deg")
    p.add_argument("--park-el", type=float, default=45.0, help="park elevation, deg")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("adsb-sim", help="write simulated ADS-B traffic (readsb aircraft.json) for testing without a receiver")
    p.add_argument("--out", help="file to write (default: adsb.source from the system file)")
    p.add_argument("--aircraft", type=int, default=8)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(func=cmd_adsb_sim)

    p = sub.add_parser("dslr", help="serve a stills camera over USB (Nikon D500, libgphoto2) for the GUI")
    p.add_argument("--sim", action="store_true", help="a simulated camera (synthetic star field)")
    p.set_defaults(func=cmd_dslr)

    p = sub.add_parser("zwo", help="serve the ZWO camera (ASI SDK) for the GUI: video, ROI, FITS frames, SER recording")
    p.add_argument("--sim", action="store_true", help="a simulated ASI224MC (star field and a moving target)")
    p.set_defaults(func=cmd_zwo)

    p = sub.add_parser("request", help="send a request to a running 'mount-svc serve'")
    rs = p.add_subparsers(dest="what", required=True)
    q = rs.add_parser("radec")
    q.add_argument("--ra", required=True)
    q.add_argument("--dec", required=True)
    q.add_argument("--minutes", type=float, default=0.0, help="0 = until it sets")
    q = rs.add_parser("tle")
    q.add_argument("--tle", required=True)
    q.add_argument("--name")
    q.add_argument("--pass", dest="pass_index", type=int, default=0)
    q.add_argument("--mode", choices=("normal", "flipped", "over_the_top"))
    q.add_argument("--allow-clock-error", action="store_true")
    q = rs.add_parser("altaz")
    q.add_argument("--az", type=float, required=True)
    q.add_argument("--el", type=float, required=True)
    q.add_argument("--hold-s", type=float, default=0.0)
    q = rs.add_parser("offsets")
    q.add_argument("--time", type=float, default=0.0, help="along-track, s")
    q.add_argument("--cross", type=float, default=0.0, help="cross-track, deg")
    q.add_argument("--az", type=float, default=0.0, help="arcsec")
    q.add_argument("--el", type=float, default=0.0, help="arcsec")
    q = rs.add_parser("stop")
    q.add_argument("--hold", action="store_true", help="keep the drives enabled, holding position")
    q = rs.add_parser("passes")
    q.add_argument("--tle", required=True)
    q.add_argument("--name")
    q.add_argument("--hours", type=float, default=24.0)
    rs.add_parser("status")
    p.set_defaults(func=cmd_request)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    try:
        return args.func(args, load_system(args.system))
    except (ConfigError, MountError) as e:
        print(f"mount-svc: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
