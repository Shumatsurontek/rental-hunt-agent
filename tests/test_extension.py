from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright, expect

_SIDEPANEL_URI = Path("chrome-extension/sidepanel.html").resolve().as_uri()
_CONTENT_CAPTURE_PATH = str(Path("chrome-extension/content-capture.js").resolve())
_RESULTS_FIXTURE = Path("tests/fixtures/results.html").read_text(encoding="utf-8")
_CORS_HEADERS = {
    "Access-Control-Allow-Headers": "authorization,content-type",
    "Access-Control-Allow-Methods": "GET,POST,PUT,OPTIONS",
    "Access-Control-Allow-Origin": "*",
}
_CHROME_STUB = """
window.chrome = {
  permissions: {contains: async () => true, request: async () => true},
  runtime: {
    reload: () => undefined,
    sendMessage: async (message) => message.type === "get-runtime-info"
      ? {extensionRuntimeVersion: "0.3.0"}
      : {processed: 0}
  },
  storage: {
    local: {
      get: async () => ({
        apiBaseUrl: "https://api.test",
        adminToken: "aaaaaaaaaaaaaaaaaaaaaaaa",
        browserScanStatus: {
          state: "queued",
          phase: "Snapshots queued for parsing and agent analysis",
          snapshots: [{
            page: 1,
            url: "https://www.seloger.com/list.htm?projects=2",
            domCandidateCount: 24,
            capturedDomCandidates: 24,
            jsonDocumentCount: 3,
            bodyChars: 12000,
            jsonDocumentChars: 24000
          }]
        }
      }),
      set: async () => undefined
    },
    onChanged: {addListener: () => undefined}
  },
  tabs: {create: async () => undefined}
};
"""


class _WatchSetupApi:
    def __init__(self) -> None:
        self.current_watch: dict[str, Any] | None = None
        self.current_preferences: dict[str, Any] | None = None
        self.mutation_requests: list[tuple[str, dict[str, Any]]] = []

    async def route(self, route: Route) -> None:
        request = route.request
        if request.method == "OPTIONS":
            await route.fulfill(status=204, headers=_CORS_HEADERS)
            return
        path = request.url.removeprefix("https://api.test")
        status, payload = self._response(path, request.method, request.post_data)
        await route.fulfill(status=status, json=payload, headers=_CORS_HEADERS)

    def _response(
        self,
        path: str,
        method: str,
        post_data: str | None,
    ) -> tuple[int, dict[str, Any]]:
        status = 200
        payload: dict[str, Any]
        if path == "/readyz":
            payload = {"status": "ready", "active_jobs": 0, "pending_notifications": 0}
        elif path == "/v1/agent/status":
            payload = {
                "model_provider": "openai",
                "model_name": "gpt-5.6-luna",
                "source_mode": "chrome_extension",
                "langsmith_tracing": True,
                "langsmith_project": "rental-hunt-agent",
                "active_jobs": 0,
                "pending_notifications": 0,
                "failed_background_tasks": [],
            }
        elif path == "/v1/watches/current":
            status = 404 if self.current_watch is None else 200
            payload = self.current_watch or {"detail": "no active watch"}
        elif path == "/v1/listings?eligible_only=true&limit=100":
            payload = {"items": [], "next_cursor": None}
        elif path == "/v1/preferences" and method == "GET":
            status = 404 if self.current_preferences is None else 200
            payload = self.current_preferences or {"detail": "preferences are not configured"}
        elif path == "/v1/preferences" and method == "PUT":
            payload = json.loads(post_data or "{}")
            self.current_preferences = payload
            self.mutation_requests.append((path, payload))
        elif path == "/v1/watches" and method == "POST":
            request_payload = json.loads(post_data or "{}")
            self.mutation_requests.append((path, request_payload))
            self.current_watch = {
                "id": "00000000-0000-0000-0000-000000000001",
                "url": request_payload["url"],
                "poll_interval_s": request_payload["poll_interval_s"],
                "configuration_version": 1,
                "enabled": True,
                "baseline_complete": False,
                "next_scan_at": "2026-08-21T16:00:00Z",
                "created_at": "2026-08-21T16:00:00Z",
                "updated_at": "2026-08-21T16:00:00Z",
            }
            status = 201
            payload = self.current_watch
        elif path.endswith("/configuration") and method == "PUT":
            if self.current_watch is None or self.current_preferences is None:
                return 404, {"detail": "watch not found"}
            request_payload = json.loads(post_data or "{}")
            self.mutation_requests.append((path, request_payload))
            requested_watch = request_payload["watch"]
            requested_preferences = request_payload["preferences"]
            material_change = (
                self.current_watch["url"] != requested_watch["url"]
                or self.current_preferences != requested_preferences
            )
            if material_change:
                self.current_watch["configuration_version"] += 1
                self.current_watch["baseline_complete"] = False
            self.current_watch["url"] = requested_watch["url"]
            self.current_watch["poll_interval_s"] = requested_watch["poll_interval_s"]
            self.current_preferences = requested_preferences
            payload = {
                "watch": self.current_watch,
                "preferences": self.current_preferences,
            }
        else:
            status = 404
            payload = {"detail": path}
        return status, payload


def test_chrome_extension_manifest_is_bounded_mv3_side_panel() -> None:
    root = Path("chrome-extension")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["side_panel"]["default_path"] == "sidepanel.html"
    assert set(manifest["permissions"]) == {
        "alarms",
        "notifications",
        "sidePanel",
        "storage",
    }
    assert "<all_urls>" not in manifest.get("host_permissions", [])
    assert set(manifest["host_permissions"]) == {"https://*.seloger.com/*"}
    assert set(manifest["optional_host_permissions"]) == {"http://*/*", "https://*/*"}
    assert (root / manifest["background"]["service_worker"]).is_file()
    assert (root / manifest["side_panel"]["default_path"]).is_file()
    assert (root / manifest["icons"]["128"]).is_file()
    content_script = manifest["content_scripts"][0]["js"][0]
    assert (root / content_script).is_file()


def test_extension_has_no_inline_javascript_or_embedded_secret() -> None:
    root = Path("chrome-extension")
    html = (root / "sidepanel.html").read_text(encoding="utf-8")
    javascript = (root / "sidepanel.js").read_text(encoding="utf-8")
    background = (root / "background.js").read_text(encoding="utf-8")
    content_capture = (root / "content-capture.js").read_text(encoding="utf-8")
    stylesheet = (root / "sidepanel.css").read_text(encoding="utf-8")

    assert '<script src="sidepanel.js"></script>' in html
    assert "<script>" not in html
    assert "sk-proj-" not in javascript
    assert "MAX_CHAT_MESSAGES = 20" in javascript
    assert "MAX_POSTAL_CODES = 50" in javascript
    assert "JOB_POLL_ATTEMPTS = 70" in javascript
    assert "PIPELINE_POLL_ATTEMPTS = 140" in javascript
    assert "/v1/chat/stream" in javascript
    assert 'apiRequest("/v1/preferences"' in javascript
    assert 'apiRequest("/v1/watches"' in javascript
    assert 'id="watch-setup-form"' in html
    assert 'value="600" selected' in html
    assert "MAX_STREAM_EVENTS = 2_002" in javascript
    assert "NOTIFICATION_PULL_MAX = 20" in background
    assert "POLL_INTERVAL_MINUTES = 1" in background
    assert "MAX_NOTIFICATION_METADATA = 100" in background
    assert "BROWSER_SCAN_PAGES_MAX = 3" in background
    assert 'EXTENSION_RUNTIME_VERSION = "0.3.0"' in background
    assert 'id="browser-snapshot-list"' in html
    assert 'id="reload-extension-button"' in html
    assert "chrome.runtime.reload()" in javascript
    assert "Explore with agent" in html
    assert "/browser-scan`" in background
    assert "JSON_DOCUMENTS_BYTES_MAX = 5_000_000" in content_capture
    assert "BODY_TEXT_CHARS_MAX = 500_000" in content_capture
    assert 'accessLevel: "TRUSTED_CONTEXTS"' in background
    assert "/v1/notifications?limit=${NOTIFICATION_PULL_MAX}" in background
    assert "/feedback`" in background
    assert "--primary: #f7a501" in stylesheet
    assert "--canvas: #eeefe9" in stylesheet
    assert "linear-gradient" not in stylesheet
    assert "box-shadow:" not in stylesheet.replace(
        "box-shadow: 0 0 0 3px rgb(59 130 246 / 20%);", ""
    )


@pytest.mark.asyncio
async def test_content_capture_extracts_bounded_structured_page() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(_RESULTS_FIXTURE)
        await page.evaluate(
            """
            window.chrome = {runtime: {onMessage: {addListener: (listener) => {
              window.captureListener = listener;
            }}}};
            """
        )
        await page.add_script_tag(path=_CONTENT_CAPTURE_PATH)
        response = await page.evaluate(
            """async () => new Promise((resolve) => {
              window.captureListener({type: "capture-seloger-page"}, {}, resolve);
            })"""
        )
        await browser.close()

    assert "error" not in response
    assert "2 annonces" in response["page"]["body_text"]
    assert len(response["page"]["json_documents"]) == 1
    assert response["page"]["dom_candidate_count"] == 0


@pytest.mark.asyncio
async def test_content_capture_extracts_current_seloger_card_markup() -> None:
    markup = """
      <base href="https://www.seloger.com/">
      <div data-testid="serp-core-classified-card-testid">
        <div data-testid="classified-card-mfe-26D7TUY8WQQB">
          <a
            data-testid="card-mfe-covering-link-testid"
            href="/annonce/location/ile-de-france/paris-75/paris-75000/26D7TUY8WQQB"
            title="Appartement à louer - Paris 11ème - 1 253 € - 2 pièces, 42 m²"
          ></a>
          <img src="https://img.seloger.com/example.jpg">
          <p>1 253 € /mois · 2 pièces · 1 chambre · 42 m² · Paris 11ème (75011)</p>
        </div>
      </div>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(markup)
        await page.evaluate(
            """
            window.chrome = {runtime: {onMessage: {addListener: (listener) => {
              window.captureListener = listener;
            }}}};
            """
        )
        await page.add_script_tag(path=_CONTENT_CAPTURE_PATH)
        response = await page.evaluate(
            """async () => new Promise((resolve) => {
              window.captureListener({type: "capture-seloger-page"}, {}, resolve);
            })"""
        )
        await browser.close()

    (candidate,) = response["page"]["dom_candidates"]
    assert candidate["title"].startswith("Appartement à louer")
    assert "1 253 € /mois" in candidate["description"]
    assert candidate["href"].endswith("26D7TUY8WQQB")


@pytest.mark.asyncio
async def test_watch_setup_validates_creates_and_edits_atomically() -> None:
    api = _WatchSetupApi()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.add_init_script(_CHROME_STUB)
        await page.route("https://api.test/**", api.route)
        await page.goto(_SIDEPANEL_URI)
        await expect(page.locator("#connection-pill")).to_have_text("Ready")
        await expect(page.locator("#browser-snapshot-state")).to_have_text("queued")
        await expect(page.locator("#browser-snapshot-list")).to_contain_text(
            "Page 1 · 24 listing links (24 captured) · 3 structured blocks"
        )

        search_url = "https://www.seloger.com/list.htm?projects=2"
        constrained_url = (
            "https://www.seloger.com/list.htm?projects=2&priceMax=2200"
            "&surfaceMin=40&roomsMin=2&order=DateDesc"
        )
        await page.locator("#watch-search-url").fill(search_url)
        await page.locator("#watch-rent-max").fill("2200")
        await page.locator("#watch-surface-min").fill("40")
        await page.locator("#watch-rooms-min").fill("2")
        await page.locator("#watch-postal-codes").fill("7501")
        await page.locator("#create-watch-button").click()
        await expect(page.locator("#watch-setup-error")).to_contain_text(
            "Invalid French postal code"
        )
        assert api.mutation_requests == []

        await page.locator("#watch-postal-codes").fill("75011, 75012, 75011")
        await page.locator("#create-watch-button").click()
        await expect(page.locator("#watch-empty")).to_be_hidden()
        await expect(page.locator("#watch-version")).to_have_text("rev 1")
        await expect(page.locator("#watch-budget-summary")).to_contain_text("€2,200")
        await expect(page.locator("#watch-frequency-summary")).to_have_text("Every 10 min")

        await page.locator("#edit-watch-button").click()
        await expect(page.locator("#watch-empty")).to_be_visible()
        await expect(page.locator("#watch-search-url")).to_have_value(constrained_url)
        await expect(page.locator("#watch-rent-max")).to_have_value("2200")
        await page.locator("#watch-rent-max").fill("2300")
        await page.locator("#watch-poll-interval").select_option("900")
        await page.locator("#watch-soft-preferences").fill("Quiet street")
        await page.locator("#create-watch-button").click()
        await expect(page.locator("#watch-empty")).to_be_hidden()
        await expect(page.locator("#watch-version")).to_have_text("rev 2")
        await expect(page.locator("#watch-budget-summary")).to_contain_text("€2,300")
        await expect(page.locator("#watch-frequency-summary")).to_have_text("Every 15 min")
        await browser.close()

    assert api.mutation_requests == [
        (
            "/v1/preferences",
            {
                "rent_eur_monthly_max": 2200,
                "surface_m2_min": "40.00",
                "rooms_min": 2,
                "furnished": "any",
                "postal_codes_allowed": ["75011", "75012"],
                "soft_preferences": [],
            },
        ),
        ("/v1/watches", {"url": constrained_url, "poll_interval_s": 600}),
        (
            "/v1/watches/00000000-0000-0000-0000-000000000001/configuration",
            {
                "preferences": {
                    "rent_eur_monthly_max": 2300,
                    "surface_m2_min": "40.00",
                    "rooms_min": 2,
                    "furnished": "any",
                    "postal_codes_allowed": ["75011", "75012"],
                    "soft_preferences": ["Quiet street"],
                },
                "watch": {
                    "url": (
                        "https://www.seloger.com/list.htm?projects=2&priceMax=2300"
                        "&surfaceMin=40&roomsMin=2&order=DateDesc"
                    ),
                    "poll_interval_s": 900,
                },
            },
        ),
    ]
