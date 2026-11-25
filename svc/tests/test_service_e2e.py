# SPDX-License-Identifier: GPL-3.0-or-later
"""mount-svc's Zenoh service end to end: a client session (as the GUI would) sends target
commands to MountService, which drives a real `mount-rt --serve` (simulated mount)."""
import math
import threading
import time

import erfa
import numpy as np
import pytest

from maxmount_svc import astro
from maxmount_svc.mount import MountClient
from maxmount_svc.proto import mpb
from maxmount_svc.service import MountService

from .conftest import ISS
from .test_e2e_mount_rt import served  # noqa: F401 - fixture

ARCSEC = math.pi / 180 / 3600


def query(gui, key, msg, reply_type):
    out = gui.query(key, msg.SerializeToString(), timeout=30)
    assert out is not None, f"no reply on {key}"
    reply = reply_type()
    reply.ParseFromString(out)
    return reply


class StatusWatch:
    def __init__(self, gui):
        self.lock = threading.Lock()
        self.last = None
        self.sub = gui.session.declare_subscriber(gui.key("svc/status"), self._on)

    def _on(self, sample):
        st = mpb.ServiceStatus()
        st.ParseFromString(sample.payload.to_bytes())
        with self.lock:
            self.last = st

    def wait(self, pred, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.last is not None and pred(self.last):
                    return self.last
            time.sleep(0.05)
        raise AssertionError(f"status never matched; last: {self.last}")


@pytest.fixture
def service(served):  # noqa: F811
    client, settings = served
    svc = MountService(settings, client)
    svc.start()
    with MountClient(settings.zenoh) as gui:  # a second session, like the GUI gateway
        watch = StatusWatch(gui)
        try:
            yield svc, client, gui, watch, settings
        finally:
            watch.sub.undeclare()
            svc.close()


def test_goto_offsets_retarget_and_stop(service):
    svc, mount, gui, watch, settings = service
    az0 = math.degrees(mount.axis("az").cmd_position_rad) % 360

    cmd = mpb.TargetCommand(goto_altaz=mpb.AltAz(az_deg=az0 + 4, el_deg=35, name="first"))
    rep = query(gui, "svc/target", cmd, mpb.TargetReply)
    assert rep.accepted, rep.message
    assert rep.plan.mode == "normal"
    watch.wait(lambda s: s.state == mpb.SERVICE_STATE_TRACKING and s.target_name == "first")

    # Axis offsets apply to the running track within about a second.
    rep = query(gui, "svc/target", mpb.TargetCommand(set_offsets=mpb.Offsets(az_arcsec=3600)), mpb.TargetReply)
    assert rep.accepted
    expected = math.radians(az0 + 4 + 1.0)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if abs(math.remainder(mount.axis("az").cmd_position_rad - expected, 2 * math.pi)) < 5 * ARCSEC:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("azimuth offset not applied")

    # Retargeting keeps the drives enabled throughout.
    disabled = []
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            if not mount.telemetry(fresh=True).enabled:
                disabled.append(time.monotonic())

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    cmd = mpb.TargetCommand(goto_altaz=mpb.AltAz(az_deg=az0 + 6, el_deg=37, name="second"))
    assert query(gui, "svc/target", cmd, mpb.TargetReply).accepted
    st = watch.wait(lambda s: s.state == mpb.SERVICE_STATE_TRACKING and s.target_name == "second")
    stop.set()
    t.join()
    assert not disabled
    assert abs(st.target_el_deg - 37) < 1e-6 and st.offsets.az_arcsec == 3600

    # Stop disables the drives and returns to idle.
    assert query(gui, "svc/target", mpb.TargetCommand(stop=mpb.Stop()), mpb.TargetReply).accepted
    watch.wait(lambda s: s.state == mpb.SERVICE_STATE_IDLE)
    deadline = time.monotonic() + 5
    while mount.telemetry(fresh=True).enabled and time.monotonic() < deadline:
        pass
    assert not mount.telemetry(fresh=True).enabled


def test_star_plans_and_bad_requests_are_rejected(service):
    _svc, _mount, gui, watch, settings = service
    site = settings.site
    # A star on the meridian now, 30 deg from the zenith towards the pole: RA = local sidereal
    # time. North of the zenith it stays far from the Sun at any time of day (the Sun avoidance
    # check would otherwise refuse it around noon).
    t1, t2 = astro.ut1_jd(time.time_ns(), site.dut1_s)
    lst = (erfa.gmst06(t1, t2, *astro.tt_jd(time.time_ns())) + math.radians(site.longitude_deg)) % (2 * math.pi)
    radec = mpb.RaDec(ra_deg=math.degrees(lst), dec_deg=site.latitude_deg + 30, duration_s=60, name="meridian star")
    rep = query(gui, "svc/target", mpb.TargetCommand(track_radec=radec), mpb.TargetReply)
    assert rep.accepted, rep.message
    assert rep.plan.mode in ("normal", "flipped")
    st = watch.wait(lambda s: s.target_name == "meridian star")
    assert abs(st.target_el_deg - 60) < 1.5  # 30 deg from the zenith, plus refraction
    assert st.site_source == "system file"

    below = mpb.RaDec(ra_deg=(math.degrees(lst) + 180) % 360, dec_deg=-80)
    rep = query(gui, "svc/target", mpb.TargetCommand(track_radec=below), mpb.TargetReply)
    assert not rep.accepted and "horizon" in rep.message

    bad = mpb.Tle(name="junk", line1="1 not a tle", line2="2 not a tle")
    rep = query(gui, "svc/target", mpb.TargetCommand(track_tle=bad), mpb.TargetReply)
    assert not rep.accepted and "TLE" in rep.message

    assert query(gui, "svc/target", mpb.TargetCommand(stop=mpb.Stop()), mpb.TargetReply).accepted


def test_passes_query(service):
    _svc, _mount, gui, _watch, settings = service
    from maxmount_svc.targets import SatelliteTarget

    epoch = SatelliteTarget(settings.site, *ISS).epoch_ns
    req = mpb.PassesRequest(tle=mpb.Tle(name="ISS", line1=ISS[0], line2=ISS[1]), hours=24, start_ns=epoch)
    rep = query(gui, "svc/passes", req, mpb.PassesReply)
    assert rep.ok, rep.message
    assert len(rep.passes) >= 2
    p = rep.passes[0]
    assert p.rise_ns < p.culmination_ns < p.set_ns
    assert {pl.mode for pl in p.plans} == {"normal", "flipped", "over_the_top"}
    assert np.isfinite(p.max_el_deg)
