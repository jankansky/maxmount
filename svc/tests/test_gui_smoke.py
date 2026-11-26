# SPDX-License-Identifier: GPL-3.0-or-later
"""Browser smoke test of the GUI: the full simulated stack (mount-rt --serve, mount-svc serve,
gateway) driven by headless Chromium. Fails on any JavaScript error. Skipped unless Playwright
and its Chromium are installed (pip install playwright && playwright install chromium)."""
import asyncio
import random
import threading

import pytest

from maxmount_svc.gateway import Gateway
from maxmount_svc.service import MountService

from .test_e2e_mount_rt import served  # noqa: F401 - fixture

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def gui_url(served):  # noqa: F811
    client, settings = served
    svc = MountService(settings, client)
    svc.start()
    port = random.randint(21000, 23000)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    gw = Gateway(settings, host="127.0.0.1", port=port)

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(gw.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(15)
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        asyncio.run_coroutine_threadsafe(gw.stop(), loop).result(15)
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
        svc.close()


def test_main_screen(gui_url):
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {e}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(gui_url)
        page.wait_for_function("() => document.querySelector('.lcd .v') && document.querySelector('.lcd .v').textContent !== '—'", timeout=15000)

        page.get_by_role("button", name="Choose target…").click()
        page.get_by_role("tab", name="Az / El").click()
        page.get_by_label("Azimuth (deg, north = 0, east = 90)").fill("15")
        page.get_by_label("Elevation (deg)").fill("40")
        page.get_by_role("button", name="Go").click()
        page.wait_for_function("() => document.querySelector('[aria-label=Target] .lcd').textContent.includes('az 15.00')", timeout=15000)

        page.get_by_role("button", name="Red").click()
        assert page.evaluate("() => document.documentElement.dataset.theme") == "red"

        page.get_by_role("button", name="STOP").click()
        page.wait_for_function("() => document.querySelector('.statusline').textContent.includes('stop')", timeout=15000)
        # No stray text from malformed templates (htm needs self-closed void elements).
        stray = page.evaluate("""() => { const w = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
          const bad = []; while (w.nextNode()) { const t = w.currentNode.textContent.trim();
          if (['section','div','label','span'].includes(t)) bad.push(t); } return bad; }""")
        assert not stray, stray
        browser.close()
    assert not errors, errors


def test_drives_screen(gui_url):
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {e}")
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(gui_url)
        page.get_by_role("button", name="Drives").click()
        assert page.evaluate("() => location.hash") == "#drives"
        page.wait_for_selector("[aria-label='Axis az'] .badge")
        page.wait_for_function("() => document.querySelector(\"[aria-label='Axis az'] .badge\").textContent !== 'no data'", timeout=15000)

        # Simulation: no arming needed. Jog az by +1 deg and wait for the event.
        az = page.locator("[aria-label='Axis az']")
        az.get_by_role("button", name="Jog az plus 1 degrees").click()
        page.wait_for_function("() => document.querySelector(\"[aria-label='Axis az'] .reply\")?.textContent.startsWith('jog az')", timeout=15000)
        page.wait_for_function("() => document.querySelector(\"[aria-label='Axis az'] .badge\").textContent === 'operation enabled'", timeout=15000)
        page.wait_for_function("() => document.querySelector('.statusline').textContent.includes('jog done')", timeout=20000)
        assert "OPERATION_ENABLED" in page.locator("[aria-label='Drive events']").text_content()

        page.locator("[aria-label='Bus and controller']").get_by_role("button", name="Disable all").click()
        page.wait_for_function("() => document.querySelector(\"[aria-label='Axis az'] .badge\").textContent !== 'operation enabled'", timeout=15000)
        stray = page.evaluate("""() => { const w = document.createTreeWalker(document.querySelector('.drives'), NodeFilter.SHOW_TEXT);
          const bad = []; while (w.nextNode()) { const t = w.currentNode.textContent.trim();
          if (['section','div','label','span','input'].includes(t)) bad.push(t); } return bad; }""")
        assert not stray, stray
        page.get_by_role("button", name="Track").click()
        assert page.locator("[aria-label='Target']").is_visible()
        browser.close()
    assert not errors, errors


def test_pass_preview(gui_url, monkeypatch):
    """Clicking a pass previews it on the Sky and Ground track views. The test TLE is from 2008,
    so the passes op is replaced by one that searches from the TLE epoch (no network needed)."""
    from maxmount_svc.proto import mpb
    from maxmount_svc.targets import SatelliteTarget

    from .conftest import ISS

    seen = []

    def passes(self, args):
        seen.append(args)
        epoch = SatelliteTarget(self.settings.site, *ISS).epoch_ns
        req = mpb.PassesRequest(tle=mpb.Tle(name="ISS", line1=ISS[0], line2=ISS[1]), hours=24, start_ns=epoch)
        rep = mpb.PassesReply()
        rep.ParseFromString(self.client.query("svc/passes", req.SerializeToString(), timeout=60))
        rows = []
        for i, p in enumerate(rep.passes):
            d = gateway_mod._msg_to_dict(p)
            d.update(name="ISS", pass_index=i, line1=ISS[0], line2=ISS[1])
            rows.append(d)
        return True, f"{len(rows)} passes (test)", {"passes": rows, "names": ["ISS"]}

    import maxmount_svc.gateway as gateway_mod
    monkeypatch.setattr(Gateway, "_passes", passes)
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {e}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(gui_url + "#passes")
        assert page.locator("[aria-label='Ground track'] canvas").is_visible()
        rows = page.locator("[aria-label=Passes] tbody tr")
        try:
            rows.first.wait_for(timeout=60000)
        except Exception:
            raise AssertionError(page.locator("[aria-label=Passes] .small").first.text_content() + " | " + page.locator(".statusline").text_content())
        rows.nth(1).click()
        page.wait_for_selector("[aria-label='Pass details'] table.kv", timeout=15000)
        detail = page.locator("[aria-label='Pass details']").text_content()
        assert "ISS" in detail and "AOS" in detail and "Lighting" in detail
        assert rows.nth(1).get_attribute("aria-selected") == "true"
        page.get_by_text("AOS to LOS").wait_for(timeout=5000)
        # A refused Track (the test machine's clock is unknown or too poor, and the 2008 pass is
        # long over) must leave the preview in place.
        rows.nth(1).get_by_role("button", name="Track").click()
        page.wait_for_function("() => document.querySelector('.statusline').textContent.includes('target: refused')", timeout=15000)
        assert page.locator("[aria-label='Pass details'] table.kv").is_visible()
        if page.locator(".clockwarn").count():
            page.locator(".clockwarn").get_by_label("Track anyway").check()
        rows.nth(1).click()  # again: clears
        page.wait_for_selector("[aria-label='Pass details'] table.kv", state="detached", timeout=5000)
        # Changing a selector searches again by itself; 'all in group' hides the watch list.
        calls = len(seen)
        page.get_by_label("Max el ≥").select_option("30")
        page.wait_for_timeout(1500)
        assert len(seen) > calls and seen[-1]["min_max_el_deg"] == 30
        page.get_by_label("Objects").select_option("all")
        page.wait_for_timeout(1500)
        assert seen[-1]["all"] is True and page.get_by_label("Add an object to the watch list").count() == 0
        page.get_by_label("Objects").select_option("watch")
        page.get_by_label("Max el ≥").select_option("0")
        page.wait_for_timeout(1500)
        rows.nth(0).press("Enter")
        page.wait_for_selector("[aria-label='Pass details'] table.kv", timeout=15000)
        page.locator("[aria-label='Pass details']").get_by_role("button", name="Clear").click()
        page.wait_for_selector("[aria-label='Pass details'] table.kv", state="detached", timeout=5000)
        # The Track screen no longer has the passes table; its sky panel shows the live target.
        page.locator("nav").get_by_role("button", name="Track").click()
        assert page.evaluate("() => location.hash") == ""
        assert not page.locator("[aria-label=Passes]").is_visible()
        browser.close()
    assert not errors, errors
