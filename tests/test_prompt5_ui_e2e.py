#!/usr/bin/env python3
"""Optional browser end-to-end test of the Stage 5 contributor + reviewer UI.

Runs headless Chrome (Playwright) against scripts/dev_server.py's handler bound to temporary
SQLite/R2 directories (the repository's public-r2/ is only read). MapLibre GL JS and pmtiles
are served from node_modules instead of unpkg, and the GBIF species-suggest call is mocked.

Skipped unless `playwright` (Python) and a Chrome/Chromium binary are available:
    pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import base64
import datetime
import functools
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - optional dependency
    sync_playwright = None

VENDOR = {
    "maplibre-gl.js": ROOT_DIR / "node_modules/maplibre-gl/dist/maplibre-gl.js",
    "maplibre-gl.css": ROOT_DIR / "node_modules/maplibre-gl/dist/maplibre-gl.css",
    "pmtiles.js": ROOT_DIR / "node_modules/pmtiles/dist/pmtiles.js",
}


def _chrome() -> str | None:
    for cand in (os.environ.get("CHROME_BIN"), "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if cand and Path(cand).exists():
            return cand
    return None


@unittest.skipUnless(sync_playwright is not None and all(p.exists() for p in VENDOR.values()),
                     "playwright and node_modules (npm install) required")
class TestStage5BrowserFlows(unittest.TestCase):
    def setUp(self) -> None:
        import backend_engine
        import build_dist
        import dev_server
        from test_prompt3_backend_and_dwca import build_test_jpeg_with_exif

        build_dist.build_dist()
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        r2 = tmp / "public-r2"
        shutil.copytree(ROOT_DIR / "public-r2", r2)
        self.backend = backend_engine.BasinmapBackend(tmp / "db.sqlite3", r2, tmp / "private-r2", enforce_turnstile=False)
        handler = functools.partial(dev_server.LocalBasinmapHandler, dist_dir=ROOT_DIR / "dist", r2_dir=r2,
                                    backend=self.backend, simulate_api_outage=False)
        self.server = dev_server.ReusableThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.photo = tmp / "exif_photo.jpg"
        self.photo.write_bytes(build_test_jpeg_with_exif(44, 3, 123, 5))
        self.bad = tmp / "not_an_image.png"
        self.bad.write_bytes(b"<html>definitely not a png file</html>")
        self.out = tmp / "shots"
        self.out.mkdir()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.backend.close()
        self.tmp.cleanup()

    def test_contributor_and_reviewer_flows_desktop_and_mobile(self) -> None:
        BASE, OUT = self.base, self.out
        errors: list = []

        def step(msg: str) -> None:
            print("  ok:", msg)

        def route_vendor(route):
            name = route.request.url.rsplit("/", 1)[-1]
            path = VENDOR.get(name)
            if not path:
                return route.abort()
            ctype = "text/css" if name.endswith(".css") else "application/javascript"
            route.fulfill(status=200, body=path.read_bytes(), headers={"content-type": ctype})

        def route_gbif(route):
            route.fulfill(status=200, headers={"content-type": "application/json", "access-control-allow-origin": "*"},
                          body=json.dumps([
                              {"key": 5204025, "canonicalName": "Oncorhynchus kisutch", "rank": "SPECIES", "family": "Salmonidae"},
                              {"key": 5204019, "canonicalName": "Oncorhynchus mykiss", "rank": "SPECIES", "family": "Salmonidae"},
                          ]))

        # 1x1 transparent PNG for basemap tiles; an empty glyph range is valid for MapLibre.
        tile_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

        def route_basemap(route):
            route.fulfill(status=200, body=tile_png, headers={"content-type": "image/png", "access-control-allow-origin": "*"})

        def route_glyphs(route):
            route.fulfill(status=200, body=b"", headers={"content-type": "application/x-protobuf", "access-control-allow-origin": "*"})

        def new_page(browser, viewport, mobile=False):
            ctx = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile, device_scale_factor=2 if mobile else 1)
            page = ctx.new_page()
            page.route(re.compile(r"https://unpkg\.com/.*"), route_vendor)
            page.route(re.compile(r"https://api\.gbif\.org/v1/species/suggest.*"), route_gbif)
            page.route(re.compile(r"https://(tiles\.maps\.eox\.at|tile\.openstreetmap\.org)/.*"), route_basemap)
            page.route(re.compile(r"https://demotiles\.maplibre\.org/font/.*"), route_glyphs)
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            return ctx, page

        with sync_playwright() as p:
            launch = {"args": ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]}
            if _chrome():
                launch["executable_path"] = _chrome()
            browser = p.chromium.launch(**launch)

            # ------------------------------------------------------------- desktop contributor
            ctx, page = new_page(browser, {"width": 1440, "height": 900})
            page.goto(BASE + "/")
            expect(page.locator("#hud-engine")).to_contain_text("MapLibre GL JS + PMTiles", timeout=20000)
            step("desktop: MapLibre + PMTiles map initialised")
            expect(page.locator("#dev-token-select")).to_be_visible()
            page.select_option("#dev-token-select", "dev-contributor-orcid")
            expect(page.locator(".user-chip")).to_contain_text("Pilot Field Contributor")
            expect(page.locator("#btn-open-review")).to_have_count(0)
            step("dev sign-in as contributor; reviewer controls hidden")

            page.click("#btn-open-drawer")
            expect(page.locator("#obs-form")).to_be_visible()
            expect(page.locator("#tab-review")).to_be_hidden()
            page.fill("#obs-name", "Oncorhynchus kis")
            page.wait_for_function("document.querySelectorAll('#obs-name-suggest option').length > 0", timeout=5000)
            page.fill("#obs-name", "Oncorhynchus kisutch")
            page.dispatch_event("#obs-name", "input")
            expect(page.locator("#obs-taxon-note")).to_contain_text("GBIF taxon 5204025")
            step("GBIF backbone suggestion resolves taxonKey 5204025")

            page.click("#btn-pick")
            expect(page.locator("#btn-pick")).to_contain_text("Click the map")
            box = page.locator("#maplibre-container canvas").bounding_box()
            page.mouse.click(box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.5)
            lat = float(page.input_value("#obs-lat"))
            lon = float(page.input_value("#obs-lon"))
            assert 43 < lat < 47 and -125 < lon < -120, (lat, lon)
            expect(page.locator("#btn-pick")).to_have_text("Pick on map")
            step(f"map picking filled coordinates {lat}, {lon} (basin click suppressed)")

            page.fill("#obs-locality", "McKenzie River at Hendricks Bridge")
            page.fill("#obs-notes", "Spawning pair, photographed from bridge")
            page.set_input_files("#obs-photos", [str(self.photo), str(self.bad)])
            expect(page.locator("#obs-file-list li")).to_have_count(2)
            expect(page.locator("#turnstile-note")).to_contain_text("bypassed in local development")
            page.click("#btn-submit-obs")
            expect(page.locator("#obs-error")).to_contain_text("Accept the dataset licence")
            step("submission blocked until the dataset licence is explicitly accepted")
            page.check("#obs-accept")
            page.screenshot(path=str(OUT / "01-desktop-form.png"))
            page.click("#btn-submit-obs")
            expect(page.locator("#obs-result .notice.ok")).to_contain_text("Saved: awaiting review", timeout=10000)
            expect(page.locator("#obs-result")).to_contain_text("not yet published to GBIF")
            result_text = page.inner_text("#obs-result")
            assert "Indexed by GBIF" not in result_text and "indexed by GBIF" not in result_text.replace("not yet", "")
            expect(page.locator(".upload-list li").nth(0)).to_contain_text("Uploaded", timeout=10000)
            expect(page.locator(".upload-list li").nth(1)).to_contain_text("Not a supported image file")
            expect(page.locator("[data-retry='1']")).to_be_visible()
            expect(page.locator("#obs-result")).to_contain_text("1 photo(s) failed")
            page.screenshot(path=str(OUT / "02-desktop-saved.png"))
            step("saved as 'awaiting review' (no GBIF claim); JPEG uploaded, fake PNG rejected with Retry offered")

            page.click("#btn-view-mine")
            expect(page.locator("#panel-mine .obs-card")).to_have_count(1)
            expect(page.locator("#panel-mine .obs-card")).to_contain_text("Awaiting review")
            expect(page.locator("#panel-mine .obs-card")).to_contain_text("Not yet published to GBIF")
            expect(page.locator("#panel-mine .obs-card")).to_contain_text("1 photo(s)")
            step("My submissions shows honest review + publication status")

            page.click("#btn-sign-out")
            expect(page.locator("#dev-token-select")).to_be_visible()
            expect(page.locator("#contrib-drawer")).to_be_hidden()
            step("sign out clears identity and closes the drawer")

            # ------------------------------------------------------------- desktop reviewer
            page.select_option("#dev-token-select", "dev-reviewer-orcid")
            expect(page.locator("#btn-open-review")).to_be_visible()
            page.click("#btn-open-review")
            expect(page.locator("#tab-review")).to_be_visible()
            card = page.locator("#queue-body .obs-card").first
            expect(card).to_contain_text("Oncorhynchus kisutch")
            expect(card).to_contain_text("1 GPS mismatch")
            card.click()
            detail = page.locator("#review-detail")
            expect(detail).to_contain_text("Pilot Field Contributor")
            expect(detail).to_contain_text("https://orcid.org/0000-0002-1825-0097")
            expect(detail).to_contain_text("beyond tolerance")
            expect(detail).to_contain_text("date differs from")
            expect(detail.locator(".thumb img")).to_have_count(1, timeout=5000)
            page.wait_for_function("document.querySelector('#review-detail .thumb img').naturalWidth >= 0")
            page.wait_for_timeout(900)
            page.screenshot(path=str(OUT / "03-desktop-review.png"))
            step("reviewer queue: verbatim coords, ORCID, private photo, EXIF GPS mismatch + date flags")

            detail.locator("summary").click()
            page.fill("#review-edit-slot input[name=scientificName]", "Oncorhynchus tshawytscha")
            page.fill("#review-edit-slot input[name=notes]", "Re-identified from photo 1")
            page.click("#review-edit-slot button[type=submit]")
            expect(page.locator("#queue-body .obs-card").first).to_contain_text("Oncorhynchus tshawytscha", timeout=5000)
            step("reviewer correction saved via PATCH (occurrenceID kept)")

            expect(page.locator("#review-detail")).to_be_visible()  # detail stays open after a correction
            page.click("#btn-needs-changes")
            expect(page.locator("#review-error")).to_contain_text("Explain what the contributor should change")
            page.fill("#review-notes", "Photo GPS is ~45 km from your pin; please confirm the location.")
            page.click("#btn-needs-changes")
            expect(page.locator("#queue-body .notice.ok")).to_contain_text("Returned to contributor", timeout=5000)
            expect(page.locator("#queue-body")).to_contain_text("Nothing in this queue")
            step("Request changes requires notes, then empties the pending queue")

            page.select_option("#queue-status", "needs_changes")
            page.locator("#queue-body .obs-card").first.click()
            page.fill("#review-notes", "Location confirmed by contributor call")
            page.click("#btn-approve")
            expect(page.locator("#queue-body .notice.ok")).to_contain_text("Approved", timeout=5000)
            page.wait_for_timeout(500)
            overlay = page.evaluate("fetch('/overlays/approved.geojson?x='+Date.now()).then(r=>r.json())")
            assert len(overlay["features"]) == 1 and overlay["features"][0]["properties"]["identifiedByID"].endswith("0001-5109-3700")
            assert overlay["features"][0]["properties"]["mediaUrls"], overlay
            step("Approve publishes the provisional overlay with EXIF-stripped media")

            # Contributor sees the reviewer's note on the returned record before approval -> check via API order
            ctx.close()

            # ------------------------------------------------------------- mobile
            ctx, page = new_page(browser, {"width": 390, "height": 844}, mobile=True)
            page.goto(BASE + "/")
            expect(page.locator("#hud-engine")).to_contain_text("MapLibre GL JS + PMTiles", timeout=20000)
            page.select_option("#dev-token-select", "dev-contributor-orcid")
            page.click("#btn-open-drawer")
            drawer = page.locator("#contrib-drawer").bounding_box()
            assert drawer["width"] >= 385 and drawer["y"] == 0, drawer
            expect(page.locator("#tab-mine")).to_be_visible()
            expect(page.locator("#drawer-close")).to_be_visible()
            assert page.input_value("#obs-date") == datetime.date.today().isoformat(), page.input_value("#obs-date")
            page.screenshot(path=str(OUT / "04-mobile-form.png"))
            page.click("#btn-pick")
            page.wait_for_timeout(400)
            sheet = page.locator("#contrib-drawer").bounding_box()
            assert sheet["y"] > 700, f"drawer should slide down while picking, got {sheet}"
            page.wait_for_timeout(700)  # smooth scroll to the map
            mbox = page.locator("#maplibre-container canvas").bounding_box()
            assert 0 <= mbox["y"] + mbox["height"] / 2 <= 844, f"map should be scrolled into view, got {mbox}"
            page.screenshot(path=str(OUT / "05-mobile-picking.png"))
            page.mouse.click(mbox["x"] + mbox["width"] / 2, mbox["y"] + mbox["height"] / 2)
            page.wait_for_timeout(400)
            assert page.input_value("#obs-lat"), "mobile pick should fill latitude"
            assert page.locator("#contrib-drawer").bounding_box()["y"] == 0, "sheet returns after picking"
            page.click("#tab-mine")
            expect(page.locator("#panel-mine .obs-card")).to_have_count(1)
            expect(page.locator("#panel-mine .obs-card")).to_contain_text("Approved")
            page.screenshot(path=str(OUT / "06-mobile-mine.png"))
            step("mobile: bottom sheet, slides away for map picking, My submissions shows Approved")
            ctx.close()

            # ------------------------------------------------------------- API outage
            browser.close()


        # Expected network noise: anonymous /api/auth/me probe (401) and the deliberately invalid upload (415).
        errors = [e for e in errors if not re.search(r"status of (401|415)", e)]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
