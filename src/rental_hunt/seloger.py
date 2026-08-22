"""Bounded Playwright sensor and deterministic SeLoger normalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from rental_hunt.bounds import BOUNDS
from rental_hunt.contracts import (
    BrowserScanCapture,
    NormalizedListing,
    ScanResult,
    canonicalize_text,
    listing_fingerprint,
)

logger = logging.getLogger(__name__)

_BLOCKED_MARKERS = (
    "access denied",
    "access is temporarily restricted",
    "captcha",
    "captcha-delivery.com",
    "datadome",
    "êtes-vous un humain",
    "unusual activity",
    "vérifiez que vous êtes humain",
)
_LOGIN_MARKERS = ("connectez-vous", "connexion à votre compte", "identifiez-vous")
_EMPTY_PATTERNS = (
    re.compile(r"\baucun(?:e)?\s+(?:annonce|résultat)", re.IGNORECASE),
    re.compile(r"(?<!\d)0\s+(?:annonce|résultat)s?\b", re.IGNORECASE),
)
_LISTING_PATH_MARKERS = ("/annonces/", "/bien/", "/location/")
_POSTAL_CODE_RE = re.compile(r"\b(\d{5})\b")
_PRICE_RE = re.compile(r"(?P<value>\d[\d\s\u00a0.]*)\s*(?:€|eur)", re.IGNORECASE)
_SURFACE_RE = re.compile(r"(?P<value>\d+(?:[,.]\d+)?)\s*m[²2]", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(?P<value>\d+)\s*pi[eè]ces?", re.IGNORECASE)
_ID_RE = re.compile(r"(?<!\d)(\d{5,})(?!\d)")
_PATH_ID_RE = re.compile(r"(?=.*\d)[A-Za-z0-9_-]{8,128}")


class SourceError(RuntimeError):
    code = "source_error"


class SourceBlockedError(SourceError):
    code = "source_blocked"


class SourceParseError(SourceError):
    code = "source_parse_failed"


class SearchTooBroadError(SourceError):
    code = "search_too_broad"


class SourceIncompleteError(SourceError):
    code = "source_incomplete"


class SourceLoginError(SourceError):
    code = "source_login_required"


class SourceNavigationError(SourceError):
    code = "source_navigation_failed"


class SourceUnsafeRedirectError(SourceError):
    code = "source_unsafe_redirect"


def validate_seloger_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("SeLoger watch URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("SeLoger watch URL must not contain user information")
    host = (parsed.hostname or "").lower()
    if host != "seloger.com" and not host.endswith(".seloger.com"):
        raise ValueError("watch URL host must be seloger.com")
    if parsed.port not in (None, 443):
        raise ValueError("SeLoger watch URL must use the default HTTPS port")
    return url


def detect_page_failure(body_text: str, document_html_prefix: str = "") -> None:
    lowered = f"{body_text}\n{document_html_prefix}".casefold()
    for marker in _BLOCKED_MARKERS:
        if marker in lowered:
            raise SourceBlockedError(f"SeLoger returned a blocked page containing {marker!r}")
    if "mot de passe" in lowered:
        for marker in _LOGIN_MARKERS:
            if marker in lowered:
                raise SourceLoginError(f"SeLoger returned a login page containing {marker!r}")


def has_explicit_empty_result(body_text: str) -> bool:
    return any(pattern.search(body_text) is not None for pattern in _EMPTY_PATTERNS)


def parse_source_total(body_text: str, fallback: int) -> int:
    horizontal_number = r"([\d \t\u00a0\u202f]+)"
    patterns = (
        re.compile(rf"{horizontal_number}[ \t\u00a0\u202f]+annonces?", re.IGNORECASE),
        re.compile(rf"{horizontal_number}[ \t\u00a0\u202f]+résultats?", re.IGNORECASE),
        re.compile(
            rf"{horizontal_number}[ \t\u00a0\u202f]+"
            r"(?:maisons?\s+et\s+appartements?|appartements?|maisons?|biens?|logements?)\s+"
            r"à\s+(?:louer|vendre)",
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        match = pattern.search(body_text)
        if match is not None:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return int(digits)
    return fallback


def parse_browser_capture(
    capture: BrowserScanCapture,
    *,
    observed_at: datetime | None = None,
) -> ScanResult:
    timestamp = observed_at or datetime.now(UTC)
    by_id: dict[str, NormalizedListing] = {}
    source_total = 0
    expected_url: str | None = None
    current_url: str | None = None
    body_text = ""

    for page_index, page in enumerate(capture.pages):
        page_url = validate_seloger_url(str(page.url))
        if page_index > 0:
            if expected_url is None:
                raise SourceIncompleteError("browser capture continued after pagination ended")
            if page_url != expected_url:
                raise SourceIncompleteError("browser capture pagination chain is discontinuous")
        detect_page_failure(page.body_text, page.document_html_prefix)
        if page.dom_candidate_count > BOUNDS.listings_per_scan_max:
            raise SearchTooBroadError("browser capture DOM result exceeds the listing bound")
        listings = parse_json_documents(page.json_documents, observed_at=timestamp)
        if not listings:
            listings = parse_dom_candidates(
                tuple(candidate.model_dump() for candidate in page.dom_candidates),
                observed_at=timestamp,
            )
        for listing in listings:
            by_id[listing.source_listing_id] = listing
        body_text = page.body_text
        source_total = max(source_total, parse_source_total(body_text, len(by_id)))
        if source_total > BOUNDS.listings_per_scan_max:
            raise SearchTooBroadError(
                f"SeLoger reports {source_total} results; narrow the search to "
                f"{BOUNDS.listings_per_scan_max} or fewer"
            )
        current_url = str(page.next_url) if page.next_url is not None else None
        if current_url is not None:
            validate_seloger_url(current_url)
        expected_url = current_url

    return _complete_scan_result(
        by_id=by_id,
        body_text=body_text,
        current_url=current_url,
        observed_at=timestamp,
        pages_scanned=len(capture.pages),
        source_total=source_total,
    )


def parse_json_documents(
    documents: Sequence[str],
    *,
    observed_at: datetime,
) -> tuple[NormalizedListing, ...]:
    if len(documents) > BOUNDS.json_scripts_max:
        raise SourceParseError("page contains more JSON scripts than the configured bound")
    roots: list[object] = []
    total_bytes = 0
    for document in documents:
        encoded_size = len(document.encode("utf-8"))
        if encoded_size > BOUNDS.json_script_bytes_max:
            raise SourceParseError("embedded JSON script exceeds the configured byte bound")
        total_bytes += encoded_size
        if total_bytes > BOUNDS.json_documents_bytes_max:
            raise SourceParseError("embedded JSON exceeds the aggregate byte bound")
        try:
            root = json.loads(document)
        except json.JSONDecodeError:
            continue
        roots.append(root)
    candidates = _walk_json_candidates(roots)
    return _normalize_candidates(candidates, observed_at=observed_at)


def parse_dom_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    observed_at: datetime,
) -> tuple[NormalizedListing, ...]:
    if len(candidates) > BOUNDS.listings_per_scan_max:
        raise SearchTooBroadError("DOM candidate count exceeds the configured listing bound")
    return _normalize_candidates(candidates, observed_at=observed_at)


def _walk_json_candidates(root: object) -> list[Mapping[str, Any]]:
    stack: list[tuple[object, int]] = [(root, 0)]
    candidates: list[Mapping[str, Any]] = []
    visited = 0
    while stack:
        node, depth = stack.pop()
        visited += 1
        if visited > BOUNDS.json_nodes_max:
            raise SourceParseError("embedded JSON tree exceeds the configured node bound")
        if depth > 32:
            raise SourceParseError("embedded JSON tree exceeds the configured depth bound")
        if isinstance(node, Mapping):
            if _looks_like_listing(node):
                candidates.append(node)
            stack.extend(
                (value, depth + 1) for value in node.values() if isinstance(value, (Mapping, list))
            )
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node if isinstance(value, (Mapping, list)))
    return candidates


def _looks_like_listing(value: Mapping[str, Any]) -> bool:
    candidate_url = _first_string(value, "url", "canonicalUrl", "href")
    type_value = _first_string(value, "@type", "type", "__typename") or ""
    has_listing_url = candidate_url is not None and any(
        marker in candidate_url.casefold() for marker in _LISTING_PATH_MARKERS
    )
    has_listing_type = type_value.casefold() in {
        "accommodation",
        "apartment",
        "house",
        "listitem",
        "offer",
        "product",
        "realestatelisting",
        "residence",
    }
    has_identity = _first_value(value, "id", "listingId", "classifiedId", "propertyId") is not None
    return has_listing_url or (has_listing_type and has_identity)


def _normalize_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    observed_at: datetime,
) -> tuple[NormalizedListing, ...]:
    by_id: dict[str, NormalizedListing] = {}
    for candidate in candidates:
        listing = _normalize_candidate(candidate, observed_at=observed_at)
        if listing is None:
            continue
        previous = by_id.get(listing.source_listing_id)
        if previous is None or _information_score(listing) > _information_score(previous):
            by_id[listing.source_listing_id] = listing
        if len(by_id) > BOUNDS.listings_per_scan_max:
            raise SearchTooBroadError("normalized result exceeds the configured listing bound")
    return tuple(by_id[key] for key in sorted(by_id))


def _normalize_candidate(
    candidate: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> NormalizedListing | None:
    raw_url = _first_string(candidate, "url", "canonicalUrl", "href")
    if raw_url is None:
        return None
    canonical_url = _canonicalize_listing_url(urljoin("https://www.seloger.com", raw_url))
    try:
        validate_seloger_url(canonical_url)
    except ValueError:
        return None

    source_listing_id = _source_listing_id(candidate, canonical_url)
    title = _first_string(candidate, "name", "title", "headline") or "Annonce SeLoger"
    description = _first_string(candidate, "description", "summary", "text") or ""
    combined_text = canonicalize_text(f"{title} {description}")
    description, description_warning = _bounded_description(description)
    address = _mapping(candidate.get("address"))
    offers = _mapping(candidate.get("offers"))

    values: dict[str, Any] = {
        "source": "seloger",
        "source_listing_id": source_listing_id,
        "canonical_url": canonical_url,
        "title": canonicalize_text(title)[:500],
        "description": description,
        "rent_eur_monthly": _parse_int(
            _first_value(candidate, "price", "rent", "amount", "priceValue")
            or _first_value(offers, "price", "lowPrice")
            or _regex_number(_PRICE_RE, combined_text)
        ),
        "surface_m2": _parse_decimal(
            _first_value(candidate, "surface", "livingArea", "area", "floorSize")
            or _regex_number(_SURFACE_RE, combined_text)
        ),
        "rooms": _parse_int(
            _first_value(candidate, "rooms", "numberOfRooms", "roomCount")
            or _regex_number(_ROOMS_RE, combined_text)
        ),
        "bedrooms": _parse_int(
            _first_value(candidate, "bedrooms", "numberOfBedrooms", "bedroomCount")
        ),
        "furnished": _parse_furnished(candidate, combined_text),
        "postal_code": _postal_code(candidate, address, combined_text),
        "city": _first_string(address, "addressLocality", "city")
        or _first_string(candidate, "city", "locality"),
        "photo_urls": _photo_urls(candidate),
        "published_at": _parse_datetime(
            _first_value(candidate, "datePosted", "publishedAt", "publicationDate")
        ),
        "observed_at": observed_at,
        "data_warnings": (description_warning,) if description_warning else (),
    }
    values["fingerprint"] = listing_fingerprint(values)
    return NormalizedListing.model_validate(values)


def _canonicalize_listing_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = re.sub(r"/{2,}", "/", parsed.path) or "/"
    return urlunsplit(("https", f"{host}{port}", path.rstrip("/") or "/", "", ""))


def _source_listing_id(candidate: Mapping[str, Any], canonical_url: str) -> str:
    explicit = _first_value(candidate, "listingId", "classifiedId", "propertyId", "id")
    if explicit is not None and isinstance(explicit, (str, int)):
        normalized = str(explicit).strip()
        if normalized:
            return normalized[:128]
    final_segment = urlparse(canonical_url).path.rstrip("/").rsplit("/", 1)[-1]
    final_segment = re.sub(r"\.html?$", "", final_segment, flags=re.IGNORECASE)
    if _PATH_ID_RE.fullmatch(final_segment) is not None:
        return final_segment
    matches = _ID_RE.findall(urlparse(canonical_url).path)
    if matches:
        return cast(str, matches[-1])
    return hashlib.sha256(canonical_url.encode()).hexdigest()[:24]


def _bounded_description(description: str) -> tuple[str, str | None]:
    normalized = canonicalize_text(description)
    if len(normalized) <= BOUNDS.description_chars_max:
        return normalized, None
    return (
        normalized[: BOUNDS.description_chars_max],
        f"description was explicitly capped at {BOUNDS.description_chars_max} characters",
    )


def _photo_urls(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    raw = _first_value(candidate, "image", "images", "photos", "photoUrls")
    values: list[object]
    if isinstance(raw, list):
        values = raw
    elif raw is None:
        values = []
    else:
        values = [raw]
    urls: list[str] = []
    for value in values:
        if isinstance(value, str):
            url = value
        elif isinstance(value, Mapping):
            url = _first_string(value, "url", "contentUrl", "src") or ""
        else:
            url = ""
        if url.startswith(("https://", "http://")):
            urls.append(url)
        if len(urls) == BOUNDS.photo_urls_max:
            break
    return tuple(dict.fromkeys(urls))


def _parse_furnished(candidate: Mapping[str, Any], text: str) -> bool | None:
    value = _first_value(candidate, "furnished", "isFurnished", "furniture")
    if isinstance(value, bool):
        return value
    lowered = text.casefold()
    if "non meublé" in lowered or "non meublée" in lowered:
        return False
    if "meublé" in lowered or "meublée" in lowered:
        return True
    return None


def _postal_code(
    candidate: Mapping[str, Any],
    address: Mapping[str, Any],
    text: str,
) -> str | None:
    value = _first_string(address, "postalCode") or _first_string(candidate, "postalCode")
    match = _POSTAL_CODE_RE.search(value or text)
    return match.group(1) if match is not None else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(mapping: Mapping[str, Any], *keys: str) -> object | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            if isinstance(value, Mapping) and "value" in value:
                return cast(object, value["value"])
            return cast(object, value)
    return None


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_value(mapping, *keys)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d[\d\s\u00a0.,]*", value)
        if match is None:
            return None
        token = re.sub(r"\s+", "", match.group(0))
        if "," in token:
            token = token.replace(".", "").replace(",", ".")
        elif "." in token:
            whole, fraction = token.rsplit(".", 1)
            token = f"{whole}.{fraction}" if len(fraction) <= 2 else token.replace(".", "")
        try:
            return int(Decimal(token))
        except InvalidOperation:
            return None
    return None


def _parse_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        value = value.get("value")
    normalized = re.sub(r"\s+", "", str(value)).replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if match is None:
        return None
    try:
        return Decimal(match.group(0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _regex_number(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group("value") if match is not None else None


def _information_score(listing: NormalizedListing) -> int:
    fields = (
        listing.rent_eur_monthly,
        listing.surface_m2,
        listing.rooms,
        listing.bedrooms,
        listing.furnished,
        listing.postal_code,
        listing.city,
        listing.published_at,
    )
    return sum(value is not None for value in fields) + bool(listing.description)


class DebugArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def capture(self, page: Page, *, code: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        prefix = self.root / f"{timestamp}-{code}"
        try:
            await page.screenshot(path=str(prefix.with_suffix(".png")), full_page=False)
            html = await page.content()
            encoded = html.encode("utf-8")
            if len(encoded) > BOUNDS.json_script_bytes_max:
                html = encoded[: BOUNDS.json_script_bytes_max].decode("utf-8", errors="ignore")
                html += "\n<!-- explicitly truncated by debug artifact bound -->"
            prefix.with_suffix(".html").write_text(html, encoding="utf-8")
        except (OSError, PlaywrightTimeoutError) as error:
            logger.warning("debug_artifact_capture_failed", extra={"error": str(error)})
        self._prune()

    def _prune(self) -> None:
        files = sorted(self.root.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
        cutoff = (
            datetime.now(UTC).timestamp()
            - timedelta(days=BOUNDS.debug_retention_days).total_seconds()
        )
        grouped: dict[str, list[Path]] = {}
        for path in files:
            grouped.setdefault(path.stem, []).append(path)
        captures = sorted(
            grouped.values(),
            key=lambda group: max(path.stat().st_mtime for path in group),
            reverse=True,
        )
        stale_groups = [
            group
            for index, group in enumerate(captures)
            if index >= BOUNDS.debug_artifacts_max
            or max(path.stat().st_mtime for path in group) < cutoff
        ]
        removed = 0
        for path in (path for group in stale_groups for path in group):
            try:
                path.unlink()
                removed += 1
            except OSError as error:
                logger.warning("debug_artifact_prune_failed", extra={"error": str(error)})
        if removed:
            logger.info("debug_artifacts_pruned", extra={"file_count": removed})


class SeLogerSource:
    def __init__(
        self,
        *,
        user_data_dir: Path,
        artifact_store: DebugArtifactStore,
        headless: bool,
    ) -> None:
        self.user_data_dir = user_data_dir
        self.artifact_store = artifact_store
        self.headless = headless

    async def scan(self, url: str) -> ScanResult:
        validate_seloger_url(url)
        observed_at = datetime.now(UTC)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with asyncio.timeout(BOUNDS.scan_timeout_s):
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        str(self.user_data_dir),
                        headless=self.headless,
                    )
                    try:
                        return await self._scan_context(context.pages[0], url, observed_at)
                    finally:
                        await context.close()
        except PlaywrightError as error:
            raise SourceNavigationError(str(error)) from error

    async def _scan_context(self, page: Page, url: str, observed_at: datetime) -> ScanResult:
        current_url: str | None = url
        by_id: dict[str, NormalizedListing] = {}
        source_total = 0
        pages_scanned = 0
        body_text = ""
        try:
            for _ in range(BOUNDS.pages_per_scan_max):
                if current_url is None:
                    break
                await page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=BOUNDS.navigation_timeout_s * 1_000,
                )
                try:
                    validate_seloger_url(page.url)
                except ValueError as error:
                    raise SourceUnsafeRedirectError(str(error)) from error
                redirected_path = urlparse(page.url).path.casefold()
                if any(marker in redirected_path for marker in ("/connexion", "/login")):
                    raise SourceLoginError("SeLoger redirected the search to a login page")
                await page.wait_for_timeout(1_000)
                body_text = await page.evaluate(
                    "limit => document.body.innerText.slice(0, limit)",
                    BOUNDS.body_text_chars_max,
                )
                document_html_prefix = await page.evaluate(
                    "limit => document.documentElement.outerHTML.slice(0, limit)",
                    BOUNDS.browser_document_html_prefix_chars_max,
                )
                detect_page_failure(body_text, document_html_prefix)
                listings = await self._parse_page(page, observed_at)
                for listing in listings:
                    by_id[listing.source_listing_id] = listing
                pages_scanned += 1
                source_total = max(source_total, parse_source_total(body_text, len(by_id)))
                if source_total > BOUNDS.listings_per_scan_max:
                    raise SearchTooBroadError(
                        f"SeLoger reports {source_total} results; narrow the search to "
                        f"{BOUNDS.listings_per_scan_max} or fewer"
                    )
                current_url = await self._next_url(page)
            return _complete_scan_result(
                by_id=by_id,
                body_text=body_text,
                current_url=current_url,
                observed_at=observed_at,
                pages_scanned=pages_scanned,
                source_total=source_total,
            )
        except SourceError as error:
            await self.artifact_store.capture(page, code=error.code)
            raise
        except PlaywrightError as error:
            await self.artifact_store.capture(page, code=SourceNavigationError.code)
            raise SourceNavigationError(str(error)) from error
        except ValueError as error:
            code = SourceParseError.code
            await self.artifact_store.capture(page, code=code)
            raise SourceParseError(str(error)) from error

    async def _parse_page(self, page: Page, observed_at: datetime) -> tuple[NormalizedListing, ...]:
        script_locator = page.locator("script[type='application/ld+json'], script#__NEXT_DATA__")
        script_count = await script_locator.count()
        if script_count > BOUNDS.json_scripts_max:
            raise SourceParseError("page contains too many structured-data scripts")
        documents = [
            await script_locator.nth(index).text_content() or "" for index in range(script_count)
        ]
        listings = parse_json_documents(documents, observed_at=observed_at)
        if listings:
            return listings
        raw_result = await page.evaluate(
            """limit => {
                const anchors = document.querySelectorAll(
                    "a[href*='/annonces/'], a[href*='/bien/'], a[href*='/location/']"
                );
                const result = {count: anchors.length, candidates: []};
                const count = Math.min(anchors.length, limit);
                for (let index = 0; index < count; index += 1) {
                    const anchor = anchors[index];
                const card = anchor.closest(
                    '[data-testid^="classified-card-mfe-"], '
                    + '[data-testid="serp-core-classified-card-testid"], '
                    + 'article, li, [class*="card"]'
                ) || anchor;
                const image = card.querySelector('img');
                result.candidates.push({
                    href: anchor.href,
                    title: anchor.getAttribute('title')
                        || (card.querySelector('h2, h3') || anchor).textContent
                        || '',
                    description: (card.innerText || '').slice(0, 20001),
                    image: image ? image.src : null,
                });
                }
                return result;
            }""",
            BOUNDS.listings_per_scan_max,
        )
        if not isinstance(raw_result, Mapping):
            raise SourceParseError("DOM fallback returned an invalid payload")
        candidate_count = raw_result.get("count")
        raw_candidates = raw_result.get("candidates")
        if not isinstance(candidate_count, int):
            raise SourceParseError("DOM fallback omitted its candidate count")
        if candidate_count > BOUNDS.listings_per_scan_max:
            raise SearchTooBroadError("DOM result exceeds the configured listing bound")
        if not isinstance(raw_candidates, list):
            raise SourceParseError("DOM fallback returned a non-list payload")
        mappings = [candidate for candidate in raw_candidates if isinstance(candidate, Mapping)]
        return parse_dom_candidates(mappings, observed_at=observed_at)

    async def _next_url(self, page: Page) -> str | None:
        value = await page.evaluate(
            """() => {
                const direct = document.querySelector('a[rel="next"]');
                if (direct && direct.href) return direct.href;
                const links = Array.from(document.querySelectorAll('a[href]'));
                const next = links.find(link => /suivant/i.test(link.textContent || ''));
                return next ? next.href : null;
            }"""
        )
        if value is None:
            return None
        if not isinstance(value, str):
            raise SourceParseError("next-page URL has an invalid type")
        validate_seloger_url(value)
        return value


def _complete_scan_result(  # noqa: PLR0913 - complete-scan invariants need all counters.
    *,
    by_id: Mapping[str, NormalizedListing],
    body_text: str,
    current_url: str | None,
    observed_at: datetime,
    pages_scanned: int,
    source_total: int,
) -> ScanResult:
    if not by_id:
        if has_explicit_empty_result(body_text):
            return ScanResult(
                listings=(),
                observed_at=observed_at,
                source_total=0,
                pages_scanned=pages_scanned,
            )
        raise SourceParseError("no listings found and the page has no explicit empty marker")
    if current_url is not None:
        raise SourceIncompleteError("result set exceeded the bounded page scan")
    if source_total > len(by_id):
        raise SourceIncompleteError("result set did not fit within the bounded page scan")
    return ScanResult(
        listings=tuple(by_id.values()),
        observed_at=observed_at,
        source_total=max(source_total, len(by_id)),
        pages_scanned=pages_scanned,
    )
