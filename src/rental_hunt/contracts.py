"""Validated API and domain contracts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from rental_hunt.bounds import BOUNDS

FurnishedPreference = Literal["required", "forbidden", "any"]
FeedbackValue = Literal["interested", "dismissed"]
NotificationKind = Literal["listing", "bootstrap_digest"]
Confidence = Literal["low", "medium", "high"]
ChatRole = Literal["user", "assistant"]


class WatchCreate(BaseModel):
    url: HttpUrl
    poll_interval_s: int = Field(
        default=BOUNDS.poll_interval_s_default,
        ge=BOUNDS.poll_interval_s_min,
        le=BOUNDS.poll_interval_s_max,
    )


class WatchPatch(BaseModel):
    enabled: bool


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    rent_eur_monthly_max: int = Field(gt=0, le=20_000)
    surface_m2_min: Decimal = Field(gt=0, le=500, decimal_places=2)
    rooms_min: int | None = Field(default=None, ge=1, le=20)
    furnished: FurnishedPreference = "any"
    postal_codes_allowed: tuple[str, ...] = Field(default=(), max_length=50)
    soft_preferences: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("postal_codes_allowed")
    @classmethod
    def validate_postal_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip() for value in values))
        for value in normalized:
            if re.fullmatch(r"\d{5}", value) is None:
                raise ValueError(f"invalid French postal code: {value!r}")
        return normalized

    @field_validator("soft_preferences")
    @classmethod
    def validate_soft_preferences(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip() for value in values))
        for value in normalized:
            if not value:
                raise ValueError("soft preferences must not be empty")
            if len(value) > 200:
                raise ValueError("each soft preference is limited to 200 characters")
        return normalized


class WatchConfigurationUpdate(BaseModel):
    watch: WatchCreate
    preferences: PreferencesUpdate


class NormalizedListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Literal["seloger"] = "seloger"
    source_listing_id: str = Field(min_length=1, max_length=128)
    canonical_url: HttpUrl
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(max_length=BOUNDS.description_chars_max)
    rent_eur_monthly: int | None = Field(default=None, gt=0, le=100_000)
    surface_m2: Decimal | None = Field(default=None, gt=0, le=10_000, decimal_places=2)
    rooms: int | None = Field(default=None, ge=1, le=100)
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    furnished: bool | None = None
    postal_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    city: str | None = Field(default=None, max_length=200)
    photo_urls: tuple[HttpUrl, ...] = Field(default=(), max_length=BOUNDS.photo_urls_max)
    published_at: datetime | None = None
    observed_at: datetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_warnings: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url_length(cls, value: HttpUrl) -> HttpUrl:
        if len(str(value)) > 600:
            raise ValueError("canonical listing URL is limited to 600 characters")
        return value

    @model_validator(mode="after")
    def validate_fingerprint(self) -> NormalizedListing:
        expected = listing_fingerprint(self)
        if self.fingerprint != expected:
            raise ValueError("fingerprint does not match normalized semantic fields")
        return self


class Eligibility(BaseModel):
    eligible: bool
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Eligibility:
        if self.eligible:
            if self.violations:
                raise ValueError("eligible result cannot contain violations")
        elif not self.violations:
            raise ValueError("ineligible result must explain at least one violation")
        return self


class ListingAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    score: int = Field(ge=0, le=100)
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=500)
    strengths: tuple[str, ...] = Field(max_length=3)
    risks: tuple[str, ...] = Field(max_length=3)
    unknowns: tuple[str, ...] = Field(max_length=3)

    @field_validator("strengths", "risks", "unknowns")
    @classmethod
    def validate_assessment_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        for value in normalized:
            if not value:
                raise ValueError("assessment items must not be empty")
            if len(value) > 240:
                raise ValueError("assessment items are limited to 240 characters")
        return normalized


class ScanResult(BaseModel):
    listings: tuple[NormalizedListing, ...] = Field(max_length=BOUNDS.listings_per_scan_max)
    observed_at: datetime
    source_total: int = Field(ge=0, le=BOUNDS.listings_per_scan_max)
    pages_scanned: int = Field(ge=1, le=BOUNDS.pages_per_scan_max)

    @model_validator(mode="after")
    def validate_complete_unique_result(self) -> ScanResult:
        source_ids = tuple(listing.source_listing_id for listing in self.listings)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("scan result contains duplicate source listing IDs")
        if self.source_total < len(self.listings):
            raise ValueError("source total cannot be smaller than the normalized result")
        return self


class BrowserDomCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    href: str = Field(min_length=1, max_length=600)
    title: str = Field(max_length=500)
    description: str = Field(max_length=BOUNDS.description_chars_max + 1)
    image: str | None = Field(default=None, max_length=600)


class BrowserPageCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    body_text: str = Field(max_length=BOUNDS.body_text_chars_max)
    document_html_prefix: str = Field(max_length=BOUNDS.browser_document_html_prefix_chars_max)
    json_documents: tuple[Annotated[str, Field(max_length=BOUNDS.json_script_bytes_max)], ...] = (
        Field(max_length=BOUNDS.json_scripts_max)
    )
    dom_candidate_count: int = Field(ge=0, le=1_000_000)
    dom_candidates: tuple[BrowserDomCandidate, ...] = Field(max_length=BOUNDS.listings_per_scan_max)
    next_url: HttpUrl | None = None

    @model_validator(mode="after")
    def validate_candidate_count(self) -> BrowserPageCapture:
        if self.dom_candidate_count < len(self.dom_candidates):
            raise ValueError("DOM candidate count cannot be smaller than captured candidates")
        return self


class BrowserScanCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_id: uuid.UUID
    watch_configuration_version: int = Field(
        ge=1,
        le=BOUNDS.watch_configuration_versions_max,
    )
    pages: tuple[BrowserPageCapture, ...] = Field(
        min_length=1,
        max_length=BOUNDS.pages_per_scan_max,
    )

    @model_validator(mode="after")
    def validate_aggregate_bounds(self) -> BrowserScanCapture:
        json_bytes = sum(
            len(document.encode("utf-8")) for page in self.pages for document in page.json_documents
        )
        if json_bytes > BOUNDS.json_documents_bytes_max:
            raise ValueError("captured structured data exceeds the aggregate byte bound")
        captured_chars = sum(
            len(page.body_text)
            + len(page.document_html_prefix)
            + sum(len(document) for document in page.json_documents)
            + sum(
                len(candidate.href)
                + len(candidate.title)
                + len(candidate.description)
                + len(candidate.image or "")
                for candidate in page.dom_candidates
            )
            for page in self.pages
        )
        if captured_chars > BOUNDS.browser_capture_chars_max:
            raise ValueError("browser capture exceeds the aggregate character bound")
        return self


class WatchView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    poll_interval_s: int
    configuration_version: int = Field(ge=1, le=BOUNDS.watch_configuration_versions_max)
    enabled: bool
    baseline_complete: bool
    next_scan_at: datetime
    created_at: datetime
    updated_at: datetime


class WatchConfigurationView(BaseModel):
    watch: WatchView
    preferences: PreferencesUpdate


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    status: str
    attempts: int
    trace_id: uuid.UUID | None
    available_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ListingView(BaseModel):
    id: uuid.UUID
    active: bool
    eligibility: Eligibility
    listing: NormalizedListing
    assessment: ListingAssessment | None
    feedback: FeedbackValue | None


class ListingPage(BaseModel):
    items: tuple[ListingView, ...]
    next_cursor: uuid.UUID | None


class ManualScanAccepted(BaseModel):
    job_id: uuid.UUID


class ChatMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    role: ChatRole
    content: str = Field(min_length=1, max_length=BOUNDS.chat_message_chars_max)


class ChatRequest(BaseModel):
    messages: tuple[ChatMessage, ...] = Field(
        min_length=1,
        max_length=BOUNDS.chat_history_messages_max,
    )

    @model_validator(mode="after")
    def validate_last_message(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the final chat message must have role 'user'")
        return self


class ChatResponse(BaseModel):
    message: str = Field(min_length=1, max_length=BOUNDS.chat_output_chars_max)
    trace_id: uuid.UUID | None
    model_provider: Literal["ollama", "openai"]
    model_name: str


class ChatStreamEvent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    type: Literal["delta", "done"]
    delta: str | None = None
    message: str | None = None
    trace_id: uuid.UUID | None = None
    model_provider: Literal["ollama", "openai"] | None = None
    model_name: str | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> ChatStreamEvent:
        if self.type == "delta":
            if not self.delta or self.message is not None:
                raise ValueError("delta events require only a non-empty delta")
            return self
        if not self.message or self.delta is not None:
            raise ValueError("done events require only a non-empty message")
        if self.model_provider is None or self.model_name is None:
            raise ValueError("done events require model identity")
        return self


class AgentStatus(BaseModel):
    model_provider: Literal["ollama", "openai"]
    model_name: str
    source_mode: Literal["chrome_extension", "playwright"]
    langsmith_tracing: bool
    langsmith_project: str | None
    notification_channel: Literal["chrome_extension"] = "chrome_extension"
    active_jobs: int = Field(ge=0, le=BOUNDS.job_queue_max)
    pending_notifications: int = Field(
        ge=0,
        le=BOUNDS.browser_notifications_pending_max,
    )
    failed_background_tasks: tuple[str, ...]


class BrowserNotificationPayload(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=BOUNDS.browser_notification_title_chars_max)
    message: str = Field(min_length=1, max_length=BOUNDS.browser_notification_message_chars_max)
    context_message: str = Field(default="Rental Hunt Agent", min_length=1, max_length=120)
    listing_id: uuid.UUID | None = None
    listing_url: HttpUrl | None = None
    score: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_listing_target(self) -> BrowserNotificationPayload:
        if (self.listing_id is None) != (self.listing_url is None):
            raise ValueError("listing ID and URL must either both be present or both be absent")
        return self


class BrowserNotificationView(BaseModel):
    id: uuid.UUID
    kind: NotificationKind
    payload: BrowserNotificationPayload
    created_at: datetime


class BrowserNotificationPage(BaseModel):
    items: tuple[BrowserNotificationView, ...] = Field(
        max_length=BOUNDS.browser_notifications_pull_max
    )


class NotificationAck(BaseModel):
    id: uuid.UUID
    status: Literal["sent"]


class FeedbackUpdate(BaseModel):
    value: FeedbackValue
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")


class FeedbackView(BaseModel):
    listing_id: uuid.UUID
    value: FeedbackValue
    updated_at: datetime


class ScanJobPayload(BaseModel):
    watch_id: uuid.UUID
    watch_configuration_version: int = Field(
        ge=1,
        le=BOUNDS.watch_configuration_versions_max,
    )
    trigger: Literal["browser", "manual", "scheduled"]
    browser_capture: BrowserScanCapture | None = None

    @model_validator(mode="after")
    def validate_browser_capture(self) -> ScanJobPayload:
        if self.trigger == "browser" and self.browser_capture is None:
            raise ValueError("browser-triggered scans require a capture")
        if self.trigger != "browser" and self.browser_capture is not None:
            raise ValueError("only browser-triggered scans accept a capture")
        return self


class AssessmentJobPayload(BaseModel):
    listing_id: uuid.UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    notify: bool
    bootstrap_scan_id: uuid.UUID | None = None


class BootstrapDigestJobPayload(BaseModel):
    scan_id: uuid.UUID
    listing_ids: tuple[uuid.UUID, ...] = Field(max_length=BOUNDS.bootstrap_assessments_max)
    eligible_total: int = Field(ge=0, le=BOUNDS.listings_per_scan_max)


class NotificationJobPayload(BaseModel):
    listing_id: uuid.UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def canonicalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).strip()


def listing_fingerprint(listing: NormalizedListing | dict[str, Any]) -> str:
    if isinstance(listing, NormalizedListing):
        values = listing.model_dump(mode="json")
    else:
        values = dict(listing)
    semantic = {
        "bedrooms": values.get("bedrooms"),
        "canonical_url": str(values["canonical_url"]),
        "city": values.get("city"),
        "description": canonicalize_text(str(values.get("description", ""))),
        "furnished": values.get("furnished"),
        "postal_code": values.get("postal_code"),
        "rent_eur_monthly": values.get("rent_eur_monthly"),
        "rooms": values.get("rooms"),
        "source": values.get("source", "seloger"),
        "source_listing_id": values["source_listing_id"],
        "surface_m2": str(values["surface_m2"]) if values.get("surface_m2") is not None else None,
        "title": canonicalize_text(str(values["title"])),
    }
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


ListingCursor = Annotated[uuid.UUID | None, Field(default=None)]
