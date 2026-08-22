from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from rental_hunt.bounds import BOUNDS
from rental_hunt.contracts import BrowserPageCapture, BrowserScanCapture
from rental_hunt.seloger import (
    DebugArtifactStore,
    SearchTooBroadError,
    SeLogerSource,
    SourceBlockedError,
    SourceLoginError,
    detect_page_failure,
    has_explicit_empty_result,
    parse_browser_capture,
    parse_dom_candidates,
    parse_json_documents,
    parse_source_total,
    validate_seloger_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _json_scripts(path: Path) -> list[str]:
    html = path.read_text(encoding="utf-8")
    return re.findall(
        r"<script[^>]+type=\"application/ld\+json\"[^>]*>(.*?)</script>",
        html,
        flags=re.DOTALL,
    )


def test_json_fixture_normalizes_and_deduplicates() -> None:
    documents = _json_scripts(FIXTURES / "results.html")
    duplicate = json.dumps(
        {
            "url": "https://www.seloger.com/annonces/locations/appartement/paris/123456789.htm",
            "id": "123456789",
            "name": "Sparse duplicate",
        }
    )

    listings = parse_json_documents([*documents, duplicate], observed_at=OBSERVED_AT)

    assert len(listings) == 2
    first = next(value for value in listings if value.source_listing_id == "123456789")
    assert first.rent_eur_monthly == 1_500
    assert first.surface_m2 == 45
    assert first.rooms == 2
    assert first.furnished is True
    assert first.postal_code == "75011"
    assert str(first.canonical_url).endswith("123456789.htm")
    assert "?cmp=" not in str(first.canonical_url)


def test_missing_fields_are_normalized_as_unknowns() -> None:
    document = json.dumps(
        {
            "@type": "RealEstateListing",
            "id": "444444444",
            "url": "/annonces/444444444.htm",
            "name": "Annonce minimale",
        }
    )

    (listing,) = parse_json_documents([document], observed_at=OBSERVED_AT)

    assert listing.rent_eur_monthly is None
    assert listing.surface_m2 is None
    assert listing.rooms is None
    assert listing.postal_code is None


def test_dom_fallback_parses_text_fields() -> None:
    listings = parse_dom_candidates(
        [
            {
                "href": "https://www.seloger.com/annonces/555555555.htm",
                "title": "Appartement 3 pièces 62 m²",
                "description": "Lyon 69003, location meublée, 1 300 €",
                "image": "https://img.seloger.com/555555555.jpg",
            }
        ],
        observed_at=OBSERVED_AT,
    )

    assert listings[0].rent_eur_monthly == 1_300
    assert listings[0].surface_m2 == 62
    assert listings[0].rooms == 3
    assert listings[0].furnished is True
    assert listings[0].postal_code == "69003"


def test_dom_fallback_parses_current_alphanumeric_listing_urls() -> None:
    listings = parse_dom_candidates(
        [
            {
                "href": (
                    "https://www.seloger.com/annonce/location/ile-de-france/paris-75/"
                    "paris-75000/26D7TUY8WQQB?serp_view=list"
                ),
                "title": (
                    "Appartement à louer - Paris 11ème arrondissement - 1\u202f253 € - "
                    "2 pièces, 1 chambre, 42 m²"
                ),
                "description": (
                    "1\u202f253 € /mois charges comprises · Appartement à louer · "
                    "2 pièces · 1 chambre · 42 m² · Paris 11ème (75011)"
                ),
            }
        ],
        observed_at=OBSERVED_AT,
    )

    assert listings[0].source_listing_id == "26D7TUY8WQQB"
    assert listings[0].rent_eur_monthly == 1_253
    assert listings[0].surface_m2 == 42
    assert listings[0].rooms == 2
    assert listings[0].postal_code == "75011"


def test_cookie_copy_does_not_hide_structured_results() -> None:
    listings = parse_json_documents(
        _json_scripts(FIXTURES / "results.html"),
        observed_at=OBSERVED_AT,
    )
    assert len(listings) == 2


def test_failure_classification_and_empty_ambiguity() -> None:
    blocked = (FIXTURES / "blocked.html").read_text(encoding="utf-8")
    with pytest.raises(SourceBlockedError):
        detect_page_failure(blocked)
    with pytest.raises(SourceBlockedError):
        detect_page_failure("Access is temporarily restricted due to unusual activity")
    with pytest.raises(SourceBlockedError):
        detect_page_failure("", '<iframe src="https://geo.captcha-delivery.com/captcha/">')
    with pytest.raises(SourceLoginError):
        detect_page_failure("Connexion à votre compte — Mot de passe")

    # A normal navigation header may invite login; it is not itself a login page.
    detect_page_failure("10 annonces — Connectez-vous — Créer une alerte")
    assert has_explicit_empty_result("Aucune annonce pour cette recherche")
    assert has_explicit_empty_result("0 résultats")
    assert not has_explicit_empty_result("10 annonces")


def test_browser_capture_reuses_deterministic_parser() -> None:
    capture = BrowserScanCapture(
        capture_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        watch_configuration_version=1,
        pages=(
            BrowserPageCapture(
                url="https://www.seloger.com/list.htm?projects=2",
                body_text="2 annonces",
                document_html_prefix="<html>",
                json_documents=tuple(_json_scripts(FIXTURES / "results.html")),
                dom_candidate_count=0,
                dom_candidates=(),
                next_url=None,
            ),
        ),
    )

    result = parse_browser_capture(capture, observed_at=OBSERVED_AT)

    assert result.source_total == 2
    assert result.pages_scanned == 1
    assert len(result.listings) == 2


def test_source_total_and_overly_broad_results() -> None:
    assert parse_source_total("1 234 annonces", 2) == 1_234
    current_total_copy = "Filtres\n3\n199 maisons et appartements à louer \N{EN DASH} 75011 (Paris)"
    assert parse_source_total(current_total_copy, 24) == 199
    candidates = [
        {
            "href": f"https://www.seloger.com/annonces/{100000000 + index}.htm",
            "title": f"Annonce {index}",
        }
        for index in range(BOUNDS.listings_per_scan_max + 1)
    ]
    with pytest.raises(SearchTooBroadError):
        parse_dom_candidates(candidates, observed_at=OBSERVED_AT)

    with pytest.raises(SearchTooBroadError, match="199 results"):
        parse_browser_capture(
            BrowserScanCapture(
                capture_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
                watch_configuration_version=1,
                pages=(
                    BrowserPageCapture(
                        url="https://www.seloger.com/classified-search?locations=POCOFR4805",
                        body_text=current_total_copy,
                        document_html_prefix="<html>",
                        json_documents=(),
                        dom_candidate_count=1,
                        dom_candidates=(
                            {
                                "href": "https://www.seloger.com/annonce/location/26D7TUY8WQQB",
                                "title": "Appartement 2 pièces, 42 m², 1 253 €",
                                "description": "Paris 75011",
                            },
                        ),
                        next_url=None,
                    ),
                ),
            ),
            observed_at=OBSERVED_AT,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.seloger.com/list.htm",
        "https://seloger.com.evil.example/list.htm",
        "https://user@www.seloger.com/list.htm",
        "https://www.seloger.com:444/list.htm",
    ],
)
def test_watch_url_ssrf_restrictions(url: str) -> None:
    with pytest.raises(ValueError):
        validate_seloger_url(url)


def test_debug_artifact_pruning_counts_captures_not_files(tmp_path: Path) -> None:
    store = DebugArtifactStore(tmp_path)
    for index in range(BOUNDS.debug_artifacts_max + 1):
        for suffix in (".html", ".png"):
            path = tmp_path / f"20260821T120000{index:06d}Z-error{suffix}"
            path.write_text("fixture", encoding="utf-8")
            timestamp = (datetime.now(UTC) + timedelta(seconds=index)).timestamp()
            os.utime(path, (timestamp, timestamp))

    store._prune()

    assert len(tuple(tmp_path.glob("*"))) == BOUNDS.debug_artifacts_max * 2


@pytest.mark.asyncio
async def test_recorded_fixture_runs_through_playwright(tmp_path: Path) -> None:
    fixture = (FIXTURES / "results.html").read_text(encoding="utf-8")
    source = SeLogerSource(
        user_data_dir=tmp_path / "profile",
        artifact_store=DebugArtifactStore(tmp_path / "debug"),
        headless=True,
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def fulfill(route: object) -> None:
            await route.fulfill(status=200, content_type="text/html", body=fixture)  # type: ignore[attr-defined]

        await page.route("https://www.seloger.com/**", fulfill)
        try:
            result = await source._scan_context(
                page,
                "https://www.seloger.com/list.htm?projects=2",
                OBSERVED_AT,
            )
        finally:
            await context.close()
            await browser.close()

    assert result.source_total == 2
    assert result.pages_scanned == 1
    assert len(result.listings) == 2
