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
