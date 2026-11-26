# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI gateway end to end: mount-rt --serve (simulated) + MountService + Gateway, driven over the
gateway's WebSocket like the browser does."""
import asyncio
import json

import aiohttp
import pytest

from maxmount_svc.gateway import STATIC_DIR, Gateway
from maxmount_svc.service import MountService

from .test_e2e_mount_rt import served  # noqa: F401 - fixture


@pytest.fixture
def stack(served):  # noqa: F811
    client, settings = served
    svc = MountService(settings, client)
    svc.start()
    try:
        yield settings
    finally:
        svc.close()


async def _session(settings, port):
    gw = Gateway(settings, host="127.0.0.1", port=port)
    await gw.start()
    return gw


async def _recv_until(ws, pred, timeout=30.0):
    async def loop():
        while True:
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.TEXT, msg
            m = json.loads(msg.data)
            if pred(m):
                return m
    return await asyncio.wait_for(loop(), timeout)


def test_gateway_serves_the_gui_and_bridges_requests(stack):
    settings = stack
    port = 18000 + __import__("random").randint(0, 2000)

    async def run():
        gw = await _session(settings, port)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"http://127.0.0.1:{port}/") as r:
                    assert r.status == 200 and "maxmount" in await r.text()
                async with http.get(f"http://127.0.0.1:{port}/app.js") as r:
                    assert r.status == 200
                async with http.get(f"http://127.0.0.1:{port}/vendor/ne_110m_land.json") as r:
                    assert r.status == 200
                async with http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                    hello = await _recv_until(ws, lambda m: m["type"] == "hello")
                    assert hello["limits"]["el"]["max_deg"] == 180
                    tel = await _recv_until(ws, lambda m: m["type"] == "telemetry")
                    assert {a["axis"] for a in tel["axes"]} == {"az", "el"}
                    assert "ra_h" in tel["sky"] and "lst_h" in tel["sky"]  # computed by the gateway
                    await _recv_until(ws, lambda m: m["type"] == "status")

                    az0 = tel["sky"]["az_deg"]
                    cmd = {"goto_altaz": {"az_deg": (az0 + 3) % 360, "el_deg": 40, "name": "gui test"}}
                    await ws.send_str(json.dumps({"type": "request", "id": 1, "op": "target", "args": cmd}))
                    rep = await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 1)
                    assert rep["ok"], rep
                    assert rep["data"]["plan"]["mode"] == "normal"
                    st = await _recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "SERVICE_STATE_TRACKING")
                    assert st["target_name"] == "gui test" and len(st["path"]) >= 2

                    await ws.send_str(json.dumps({"type": "request", "id": 2, "op": "target",
                                                  "args": {"set_offsets": {"az_arcsec": 30}}}))
                    assert (await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 2))["ok"]
                    await _recv_until(ws, lambda m: m["type"] == "status" and m["offsets"]["az_arcsec"] == 30)

                    await ws.send_str(json.dumps({"type": "request", "id": 3, "op": "stop"}))
                    rep = await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 3)
                    assert rep["ok"], rep
                    await _recv_until(ws, lambda m: m["type"] == "telemetry" and not m["enabled"])

                    await ws.send_str(json.dumps({"type": "request", "id": 4, "op": "bogus"}))
                    rep = await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 4)
                    assert not rep["ok"] and "unknown op" in rep["message"]
        finally:
            await gw.stop()

    asyncio.run(run())


def test_static_dir_exists():
    assert (STATIC_DIR / "index.html").exists() and (STATIC_DIR / "vendor" / "preact-htm-standalone.module.js").exists()


def test_drives_ops_enable_disable_and_jog(stack):
    """The Drives screen's operations: axis configuration in hello, enable/disable straight to
    mount-rt, and jogs through mount-svc (relative, absolute, soft-limit refusal, busy refusal)."""
    settings = stack
    port = 18000 + __import__("random").randint(2001, 4000)

    async def run():
        gw = await _session(settings, port)
        rid = 0

        async def req(ws, op, args=None):
            nonlocal rid
            rid += 1
            await ws.send_str(json.dumps({"type": "request", "id": rid, "op": op, "args": args or {}}))
            return await _recv_until(ws, lambda m, i=rid: m["type"] == "reply" and m["id"] == i)

        def axis(m, name):
            return next(a for a in m["axes"] if a["axis"] == name)

        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                hello = await _recv_until(ws, lambda m: m["type"] == "hello")
                names = [a["name"] for a in hello["axes"]]
                assert names == ["az", "el"]
                az_cfg = hello["axes"][0]
                assert az_cfg["commissioned"] and az_cfg["limits"]["max_deg"] == 270
                assert hello["fieldbus"]["backend"] == "sim"
                tel = await _recv_until(ws, lambda m: m["type"] == "telemetry")
                assert {"sw", "error_code", "vendor_error", "buffered", "cmd_vel_deg_s"} <= set(axis(tel, "az"))

                rep = await req(ws, "enable", {"axis": "el"})
                assert rep["ok"], rep
                await _recv_until(ws, lambda m: m["type"] == "telemetry" and axis(m, "el")["state"] == "OPERATION_ENABLED")
                rep = await req(ws, "disable", {"axis": "el"})
                assert rep["ok"], rep
                await _recv_until(ws, lambda m: m["type"] == "telemetry" and axis(m, "el")["state"] != "OPERATION_ENABLED")

                # Relative jog: enables the axis, moves, then holds.
                start = axis(await _recv_until(ws, lambda m: m["type"] == "telemetry"), "az")["act_deg"]
                rep = await req(ws, "jog", {"axis": "az", "by_deg": 2.0, "vel_deg_s": 2, "acc_deg_s2": 4})
                assert rep["ok"], rep
                assert (await req(ws, "jog", {"axis": "az", "by_deg": 1.0}))["ok"] is False  # still moving
                st = await _recv_until(ws, lambda m: m["type"] == "status" and m["target_kind"] == "jog")
                assert st["target_name"].startswith("jog az")
                await _recv_until(ws, lambda m: m["type"] == "status" and m["message"].startswith("jog done"), timeout=20)
                m = await _recv_until(ws, lambda m: m["type"] == "telemetry")
                assert abs(axis(m, "az")["act_deg"] - (start + 2.0)) < 0.01
                assert axis(m, "az")["state"] == "OPERATION_ENABLED"  # holding

                # Absolute jog and the soft limits.
                rep = await req(ws, "jog", {"axis": "az", "absolute": True, "to_deg": start + 1.0, "vel_deg_s": 2})
                assert rep["ok"], rep
                await _recv_until(ws, lambda m: m["type"] == "status" and m["message"].startswith("jog done"), timeout=20)
                rep = await req(ws, "jog", {"axis": "az", "absolute": True, "to_deg": 300})
                assert not rep["ok"] and "soft limits" in rep["message"]
                rep = await req(ws, "jog", {"axis": "tilt", "by_deg": 1})
                assert not rep["ok"] and "unknown axis" in rep["message"]

                # Refused while mount-svc tracks a target.
                az0 = (await _recv_until(ws, lambda m: m["type"] == "telemetry"))["sky"]["az_deg"]
                rep = await req(ws, "target", {"goto_altaz": {"az_deg": (az0 + 2) % 360, "el_deg": 40}})
                assert rep["ok"], rep
                rep = await req(ws, "jog", {"axis": "el", "by_deg": 1})
                assert not rep["ok"] and "target is active" in rep["message"]

                assert (await req(ws, "stop"))["ok"]
                await _recv_until(ws, lambda m: m["type"] == "telemetry" and not m["enabled"])
                assert (await req(ws, "fault_reset"))["ok"]
        finally:
            await gw.stop()

    asyncio.run(run())


def test_pass_preview(stack):
    """Clicking a pass in the GUI asks for its ground track and sky path."""
    import numpy as np

    from maxmount_svc.planner import find_passes
    from maxmount_svc.targets import SatelliteTarget

    from .conftest import ISS

    settings = stack
    sat = SatelliteTarget(settings.site, *ISS, name="ISS")
    passes = find_passes(sat, sat.epoch_ns, sat.epoch_ns + 24 * 3600 * 10**9, np.radians(settings.tracking.horizon_min_el_deg))
    p = next(q for q in passes if q.rise_ns > sat.epoch_ns)  # a whole pass
    port = 18000 + __import__("random").randint(2001, 4000)

    async def run():
        gw = await _session(settings, port)
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                args = {"name": "ISS", "line1": ISS[0], "line2": ISS[1], "rise_ns": str(p.rise_ns),
                        "culmination_ns": str(p.culmination_ns), "set_ns": str(p.set_ns)}
                await ws.send_str(json.dumps({"type": "request", "id": 1, "op": "pass_preview", "args": args}))
                rep = await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 1)
                assert rep["ok"], rep
                d = rep["data"]
                g = d["ground"]
                # 15 min either side of the pass at 20 s steps, plus the pass itself.
                assert len(g["points"]) > 90
                assert {f for *_ll, f in g["points"]} == {0, 1}
                assert g["points"][0][2] == 0 and g["points"][-1][2] == 0
                assert 9 < g["visibility_deg"] < 13  # ISS at ~350 km above a 10 deg mask: ~11 deg of arc
                for lon, lat in (g["aos"], g["los"], g["now"]):
                    assert -180 <= lon <= 180 and -52 <= lat <= 52  # ISS inclination 51.6 deg
                sky = d["sky"]
                mask = settings.tracking.horizon_min_el_deg
                assert abs(sky[0][1] - mask) < 0.5 and abs(sky[-1][1] - mask) < 0.5
                assert abs(max(e for _a, e, _s in sky) - np.degrees(p.max_el)) < 0.2
                assert d["sunlit"] in ("sunlit", "in shadow", "partly sunlit") and d["sunlit"] in rep["message"]

                await ws.send_str(json.dumps({"type": "request", "id": 2, "op": "pass_preview", "args": {"name": "x"}}))
                rep = await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 2)
                assert not rep["ok"]
        finally:
            await gw.stop()

    asyncio.run(run())
