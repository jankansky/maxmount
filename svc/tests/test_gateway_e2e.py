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


def test_track_anyway_passes_the_clock_override(stack):
    """The GUI's 'Track anyway' sends allow_clock_error with track_tle; mount-svc must then skip
    the clock gate (the 2008 test TLE may still be refused for other reasons)."""
    from .conftest import ISS

    settings = stack
    port = 18000 + __import__("random").randint(4001, 6000)

    async def run():
        gw = await _session(settings, port)
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                replies = []
                for i, allow in enumerate((False, True)):
                    args = {"track_tle": {"name": "ISS", "line1": ISS[0], "line2": ISS[1]}, "allow_clock_error": allow}
                    await ws.send_str(json.dumps({"type": "request", "id": i, "op": "target", "args": args}))
                    replies.append(await _recv_until(ws, lambda m, i=i: m["type"] == "reply" and m["id"] == i))
                if "clock" in replies[0]["message"]:
                    assert "clock" not in replies[1]["message"], replies[1]
                await ws.send_str(json.dumps({"type": "request", "id": 9, "op": "stop"}))
                await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == 9)
        finally:
            await gw.stop()

    asyncio.run(run())


def test_passes_all_in_group_and_plan_on_demand(stack):
    """'All in group' finds passes for every object without planning them; pass_plan plans one;
    min_max_el_deg filters; oversized groups are refused."""
    from maxmount_svc import gateway as gw_mod
    from maxmount_svc.targets import SatelliteTarget

    from .conftest import ISS

    settings = stack
    epoch = SatelliteTarget(settings.site, *ISS).epoch_ns
    port = 18000 + __import__("random").randint(6001, 8000)

    async def run():
        gw = await _session(settings, port)
        groups = {"stations": {"ISS": ISS, "ISS TWIN": ISS}, "starlink": {f"S{i}": ISS for i in range(gw_mod.ALL_MAX + 1)}}
        gw.tles.group = lambda g: (groups[g], "test")
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                async def req(i, op, args):
                    await ws.send_str(json.dumps({"type": "request", "id": i, "op": op, "args": args}))
                    return await _recv_until(ws, lambda m: m["type"] == "reply" and m["id"] == i, timeout=90)

                base = {"group": "stations", "all": True, "hours": 24, "start_ns": str(epoch)}
                r = await req(1, "passes", base)
                assert r["ok"], r
                rows = r["data"]["passes"]
                # Identical orbits (like docked modules) are merged into one row.
                assert rows and {x["name"] for x in rows} == {"ISS"} and all(x["also"] == ["ISS TWIN"] for x in rows)
                assert all(x["planned"] is False and not x["chosen"] for x in rows)
                assert [int(x["rise_ns"]) for x in rows] == sorted(int(x["rise_ns"]) for x in rows)
                assert r["data"]["names"] == ["ISS", "ISS TWIN"]

                high = await req(2, "passes", {**base, "min_max_el_deg": 40})
                assert high["ok"] and 0 < len(high["data"]["passes"]) < len(rows)
                assert all(x["max_el_deg"] >= 40 for x in high["data"]["passes"])

                p = rows[0]
                r = await req(3, "pass_plan", {k: p[k] for k in ("name", "line1", "line2", "rise_ns", "set_ns")})
                assert r["ok"], r
                planned = r["data"]["pass"]
                assert planned["planned"] is True and planned["plans"]
                # Planning keeps the lighting (mount-svc's reply has none of its own).
                assert abs(planned["sunlit_frac"] - p["sunlit_frac"]) < 0.05 and abs(planned["sun_el_deg"] - p["sun_el_deg"]) < 0.5
                assert abs(int(planned["rise_ns"]) - int(p["rise_ns"])) < 2 * 10**9

                watch = await req(4, "passes", {"group": "stations", "names": ["iss twin", "NOPE"], "hours": 24, "start_ns": str(epoch)})
                assert watch["ok"] and watch["data"]["passes"] and all(x["planned"] for x in watch["data"]["passes"])
                assert {x["name"] for x in watch["data"]["passes"]} == {"ISS TWIN"} and "NOPE" in watch["message"]

                # Lighting: every row says how much of the pass is sunlit and where the Sun is.
                assert all(0 <= x["sunlit_frac"] <= 1 and -90 <= x["sun_el_deg"] <= 90 for x in rows)
                lit = (await req(6, "passes", {**base, "lighting": "sunlit"}))["data"]["passes"]
                dark = (await req(7, "passes", {**base, "lighting": "dark_sky"}))["data"]["passes"]
                assert all(x["sunlit_frac"] > 0 for x in lit) and len(lit) == sum(x["sunlit_frac"] > 0 for x in rows)
                assert all(x["sunlit_frac"] > 0 and x["sun_el_deg"] < -6 for x in dark) and len(dark) <= len(lit)
                assert not (await req(8, "passes", {**base, "lighting": "bogus"}))["ok"]

                big = await req(5, "passes", {**base, "group": "starlink"})
                assert not big["ok"] and "too many" in big["message"]
        finally:
            await gw.stop()

    asyncio.run(run())


def test_watch_terms_match_names_and_catalog_numbers():
    from maxmount_svc.gateway import WATCH_MAX, _match_names

    l1 = "1 {:05d}U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
    sats = {"ISS (ZARYA)": (l1.format(25544), ""), "ISS (NAUKA)": (l1.format(49044), ""),
            "CSS (TIANHE)": (l1.format(48274), ""), "HST": (l1.format(20580), "")}
    assert _match_names(sats, ["ISS"])[0] == ["ISS (ZARYA)", "ISS (NAUKA)"]
    assert _match_names(sats, ["iss (zarya)"])[0] == ["ISS (ZARYA)"]
    assert _match_names(sats, ["HST"])[0] == ["HST"]                     # exact name wins
    assert _match_names(sats, ["20580", "025544"])[0] == ["HST", "ISS (ZARYA)"]
    names, notes = _match_names(sats, ["ISS", "ISS (ZARYA)", "nope", " "])
    assert names == ["ISS (ZARYA)", "ISS (NAUKA)"] and notes["missing"] == ["nope"]
    many = {f"STARLINK-{i}": (l1.format(i), "") for i in range(WATCH_MAX + 1)}
    names, notes = _match_names(many, ["starlink"])
    assert names == [] and notes["capped"] == "starlink" and notes["count"] == WATCH_MAX + 1
