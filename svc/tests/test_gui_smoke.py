# SPDX-License-Identifier: GPL-3.0-or-later
"""Browser smoke test of the GUI: the full simulated stack (mount-rt --serve, mount-svc serve,
gateway) driven by headless Chromium. Fails on any JavaScript error. Skipped unless Playwright
and its Chromium are installed (pip install playwright && playwright install chromium)."""
import asyncio
import random
import threading

import pytest

from maxmount_svc.gateway import Gateway
from maxmount_svc.satcat import OVERRIDES, SatCatalog
from maxmount_svc.service import MountService

from .test_e2e_mount_rt import served  # noqa: F401 - fixture
from .test_satcat import SAMPLE

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
    gw.satcat = SatCatalog.from_text(SAMPLE, overrides=OVERRIDES)  # no download in tests

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

        # The imaging profile picker (sim-altaz: the one default profile) and its pixel scale.
        assert page.get_by_label("Imaging profile").is_visible()
        assert "0.57″/px" in page.locator("label.imaging").text_content()
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
            d.update(name="ISS", pass_index=i, line1=ISS[0], line2=ISS[1], planned=True)
            self._add_lighting(d)
            if args.get("all"):  # like the real all-in-group search: planned on selection
                d.update(planned=False, chosen={}, plans=[])
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
        assert "109 × 73 m" in detail and "standard -2.5" in detail or "not sunlit" in detail
        assert rows.nth(1).get_attribute("aria-selected") == "true"
        page.get_by_text("AOS to LOS").wait_for(timeout=5000)
        # A refused Track (the test machine's clock is unknown or too poor, and the 2008 pass is
        # long over) must leave the preview in place.
        rows.nth(1).get_by_role("button", name="Track").click()
        page.wait_for_function("() => document.querySelector('.statusline').textContent.includes('target: refused')", timeout=15000)
        assert page.locator("[aria-label='Pass details'] table.kv").is_visible()
        if page.locator("[aria-label=Passes] .clockwarn").count():
            page.locator("[aria-label=Passes] .clockwarn").get_by_label("Track anyway").check()
        rows.nth(1).click()  # again: clears
        page.wait_for_selector("[aria-label='Pass details'] table.kv", state="detached", timeout=5000)
        # Changing a selector searches again by itself; 'all in group' hides the watch list.
        sizes = page.locator("[aria-label=Passes]").get_by_label("Size").locator("option").all_text_contents()
        assert sizes[0] == "any" and "px" in sizes[1]
        calls = len(seen)
        page.locator("[aria-label=Passes]").get_by_label("Size").select_option("20")
        page.wait_for_timeout(1500)
        assert seen[-1]["min_size_arcsec"] == 20
        page.locator("[aria-label=Passes]").get_by_label("Size").select_option("any")
        page.wait_for_timeout(1000)
        page.get_by_label("Max el ≥").select_option("30")
        page.wait_for_timeout(1500)
        assert len(seen) > calls and seen[-1]["min_max_el_deg"] == 30
        page.get_by_label("Objects").select_option("all")
        page.wait_for_timeout(1500)
        assert seen[-1]["all"] is True and page.get_by_label("Add an object to the watch list").count() == 0
        page.get_by_label("Objects").select_option("watch")
        page.get_by_label("Max el ≥").select_option("0")
        page.wait_for_timeout(1500)
        page.get_by_label("Objects").select_option("all")
        page.wait_for_timeout(1500)
        lit_before = rows.nth(0).locator("td").nth(4).text_content()
        rows.nth(0).click()
        page.wait_for_function("() => !document.querySelector('[aria-label=Passes] tbody tr td:nth-child(8)').textContent.includes('—')", timeout=15000)
        assert rows.nth(0).locator("td").nth(4).text_content() == lit_before != "?"
        rows.nth(0).click()
        page.get_by_label("Objects").select_option("watch")
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


def test_conjunctions_tab(gui_url, monkeypatch, served):  # noqa: F811
    """The Conjunctions tab lists SOCRATES events visible from the site and previews one with
    both objects' paths. Data are injected (2008 ISS elements), so no network is needed."""
    from maxmount_svc import socrates

    from .conftest import ISS
    from .test_socrates import PAIR_OMM, iss_conjunctions

    _client, settings = served
    text, p = iss_conjunctions(settings.site, settings.tracking.horizon_min_el_deg)
    orig_conj = Gateway._conjunctions

    def conjunctions(self, args):
        self.socrates = getattr(self, "_test_soc", None) or socrates.Socrates.from_text(text)
        self._test_soc = self.socrates
        self.socrates.pair = lambda a, b: socrates.omm_to_tles(PAIR_OMM)
        if not getattr(self, "_test_tles", False):
            real = self.tles.group
            self.tles.group = lambda g: ({"ISS (ZARYA)": ISS}, "test") if g == "active" else real(g)
            self._test_tles = True
        return orig_conj(self, {**args, "start_ns": str(p.rise_ns - 3600 * 10**9), "hours": 3})

    monkeypatch.setattr(Gateway, "_conjunctions", conjunctions)
    errors = []
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {e}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(gui_url + "#passes")
        page.get_by_role("tab", name="Conjunctions (SOCRATES)").first.click()
        conj = page.locator("[aria-label=Conjunctions]")
        rows = conj.locator("tbody tr:not(.empty)")
        conj.get_by_label("Lighting").select_option("any")
        conj.get_by_label("co-orbiting pairs").check()
        rows.first.wait_for(timeout=30000)
        assert "ISS (ZARYA)" in rows.first.text_content() and "TEST DEBRIS" in rows.first.text_content()
        rows.first.click()
        detail = page.locator("[aria-label='Conjunction details']")
        detail.wait_for(timeout=30000)
        assert "(99001)" in detail.text_content() and "apart at" in detail.text_content()
        page.get_by_text("✕: closest approach").wait_for(timeout=5000)
        # Track is refused here (old pass, poor test clock) but must not break anything.
        rows.first.get_by_role("button", name="Track").click()
        page.wait_for_function("() => document.querySelector('.statusline').textContent.includes('target')", timeout=30000)
        detail.get_by_role("button", name="Clear").click()
        page.wait_for_selector("[aria-label='Conjunction details']", state="detached", timeout=5000)
        page.get_by_role("tab", name="Upcoming passes").first.click()
        assert page.locator("[aria-label=Passes]").is_visible() and not conj.is_visible()
        browser.close()
    assert not errors, errors


def test_aircraft_tab(gui_url, monkeypatch, served, tmp_path):  # noqa: F811
    """The Aircraft tab with simulated ADS-B traffic: the list refreshes, a click selects an
    aircraft and shows its details, and the map and sky draw without errors."""
    import threading

    from maxmount_svc import adsb

    _client, settings = served
    path = tmp_path / "aircraft.json"
    stop = threading.Event()
    sim = threading.Thread(target=adsb.run_simulator, args=(settings.site, str(path)),
                           kwargs={"n": 12, "period_s": 0.5, "stop": stop.is_set}, daemon=True)
    sim.start()
    orig = Gateway._aircraft

    def aircraft(self, args):
        if self.adsb.source != str(path):
            self.adsb = adsb.AdsbSource(str(path))
        return orig(self, args)

    monkeypatch.setattr(Gateway, "_aircraft", aircraft)
    errors = []
    try:
        with playwright.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"no Chromium for Playwright: {e}")
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(gui_url + "#passes")
            page.get_by_role("tab", name="Aircraft (ADS-B)").first.click()
            panel = page.locator("[aria-label=Aircraft]")
            panel.get_by_label("Which aircraft").select_option("all")
            panel.get_by_label("Aircraft range").select_option("200")
            rows = panel.locator("tbody tr:not(.empty)")
            rows.first.wait_for(timeout=15000)
            assert "SIM" in rows.first.text_content()
            rows.first.click()
            page.wait_for_selector("[aria-label='Aircraft details'] table.kv", timeout=10000)
            assert "ICAO" in page.locator("[aria-label='Aircraft details']").text_content()
            page.get_by_text("Arrows: aircraft along their track").wait_for(timeout=5000)
            page.wait_for_timeout(2500)  # a couple of refreshes keep the selection
            assert rows.locator("[aria-selected=true]").count() == 0 and panel.locator("tr.selected").count() == 1
            page.get_by_role("tab", name="Upcoming passes").first.click()
            assert page.locator("[aria-label=Passes]").is_visible()
            browser.close()
    finally:
        stop.set()
    assert not errors, errors
