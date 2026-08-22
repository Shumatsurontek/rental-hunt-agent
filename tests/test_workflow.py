from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rental_hunt.contracts import (
    BrowserPageCapture,
    BrowserScanCapture,
    ListingAssessment,
    NormalizedListing,
    PreferencesUpdate,
    ScanResult,
    WatchCreate,
)
from rental_hunt.database import session_scope
from rental_hunt.models import (
    JobRecord,
    ListingRecord,
    NotificationRecord,
    ScanRunRecord,
    WatchRecord,
)
from rental_hunt.repository import (
    create_watch,
    enqueue_browser_scan,
    enqueue_manual_scan,
    put_preferences,
)
from rental_hunt.seloger import SourceBlockedError
from rental_hunt.services import JobWorker


class FakeTracing:
    enabled = False

    async def arun(
        self,
        _name: str,
        *,
        inputs: Mapping[str, object],
        operation: Callable[[], Awaitable[str]],
        tags: tuple[str, ...] = (),
        metadata: Mapping[str, object] | None = None,
        run_id: uuid.UUID | None = None,
    ) -> tuple[str, None]:
        del inputs, tags, metadata, run_id
        return await operation(), None


class FakeSource:
    def __init__(self) -> None:
        self.outcomes: list[ScanResult | Exception] = []

    async def scan(self, _url: str) -> ScanResult:
        if not self.outcomes:
            raise AssertionError("test source has no queued outcome")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeAgent:
    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    async def assess(
        self,
        _listing: NormalizedListing,
        _preferences: PreferencesUpdate,
    ) -> ListingAssessment:
        self.calls += 1
        if self.fail:
            raise RuntimeError("model unavailable")
        return ListingAssessment(
            score=80,
            confidence="high",
            summary="Good documented fit.",
            strengths=("quiet",),
            risks=(),
            unknowns=(),
        )


def _result(*listings: NormalizedListing) -> ScanResult:
    return ScanResult(
        listings=tuple(listings),
        observed_at=datetime.now(UTC),
        source_total=len(listings),
        pages_scanned=1,
    )


async def _make_pending_jobs_due(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as session:
        await session.execute(
            update(JobRecord)
            .where(JobRecord.status == "pending")
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def _enqueue_scan(
    factory: async_sessionmaker[AsyncSession],
    watch_id: uuid.UUID,
) -> None:
    async with session_scope(factory) as session:
        await enqueue_manual_scan(session, watch_id)


async def _setup(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_scope(factory) as session:
        await put_preferences(
            session,
            PreferencesUpdate(
                rent_eur_monthly_max=2_000,
                surface_m2_min=Decimal("30"),
                rooms_min=1,
                furnished="any",
                soft_preferences=("quiet",),
            ),
        )
        watch = await create_watch(
            session,
            WatchCreate(url="https://www.seloger.com/list.htm", poll_interval_s=600),
        )
        return watch.id


async def _notifications(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[NotificationRecord, ...]:
    async with session_scope(factory) as session:
        return tuple(
            (
                await session.scalars(
                    select(NotificationRecord).order_by(NotificationRecord.created_at)
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_browser_capture_job_is_durable_idempotent_and_bypasses_playwright(
    database: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = database
    watch_id = await _setup(factory)
    capture = BrowserScanCapture(
        capture_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        watch_configuration_version=1,
        pages=(
            BrowserPageCapture(
                url="https://www.seloger.com/list.htm?captured=1",
                body_text="1 annonce",
                document_html_prefix="<html>",
                json_documents=(
                    json.dumps(
                        {
                            "@type": "RealEstateListing",
                            "id": "777777777",
                            "url": "https://www.seloger.com/annonces/777777777.htm",
                            "name": "Captured apartment",
                            "description": "Paris 75011, 40 m², 2 pièces, 1 500 €",
                        }
                    ),
                ),
                dom_candidate_count=0,
                dom_candidates=(),
                next_url=None,
            ),
        ),
    )
    async with session_scope(factory) as session:
        first = await enqueue_browser_scan(session, watch_id, capture)
        duplicate = await enqueue_browser_scan(session, watch_id, capture)
        assert duplicate.id == first.id

    source = FakeSource()
    worker = JobWorker(
        session_factory=factory,
        source=source,  # type: ignore[arg-type]
        agent=FakeAgent(),  # type: ignore[arg-type]
        model_provider="openai",
        model_name="test-model",
        tracing=FakeTracing(),  # type: ignore[arg-type]
    )
    assert await worker.run_once() == 1

    async with session_scope(factory) as session:
        job = await session.get(JobRecord, first.id)
        listing = await session.scalar(
            select(ListingRecord).where(ListingRecord.source_listing_id == "777777777")
        )
        watch = await session.get(WatchRecord, watch_id)
        assert job is not None and job.status == "succeeded"
        assert job.payload["watch_id"] == str(watch_id)
        assert job.payload["trigger"] == "browser"
        assert job.payload["capture_id"] == str(capture.capture_id)
        assert job.payload["captured_pages"] == 1
        assert job.payload["watch_configuration_version"] == 1
        assert job.payload["snapshots"] == [
            {
                "body_chars": len("1 annonce"),
                "captured_dom_candidates": 0,
                "document_html_chars": len("<html>"),
                "dom_candidate_count": 0,
                "json_document_count": 1,
                "json_document_chars": len(capture.pages[0].json_documents[0]),
                "page": 1,
                "url": "https://www.seloger.com/list.htm?captured=1",
            }
        ]
        assert "browser_capture" not in job.payload
        assert listing is not None and listing.payload["title"] == "Captured apartment"
        assert watch is not None and watch.url == "https://www.seloger.com/list.htm"
    assert source.outcomes == []


@pytest.mark.asyncio
async def test_end_to_end_bootstrap_new_match_restart_and_failures(  # noqa: PLR0915
    database: tuple[object, async_sessionmaker[AsyncSession]],
    listing_factory: Callable[..., NormalizedListing],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = database
    watch_id = await _setup(factory)
    source = FakeSource()
    agent = FakeAgent()
    tracing = FakeTracing()
    worker = JobWorker(
        session_factory=factory,
        source=source,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
        model_provider="openai",
        model_name="test-model",
        tracing=tracing,  # type: ignore[arg-type]
    )

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("rental_hunt.services.asyncio.sleep", no_sleep)

    baseline = listing_factory()
    source.outcomes.append(_result(baseline))
    await _enqueue_scan(factory, watch_id)
    assert await worker.run_once() == 1  # Scan creates assessment + digest jobs atomically.
    assert await worker.run_once() == 2
    # The digest either observes the concurrent assessment as running and defers,
    # or observes its committed completion and prepares the outbox item in the same batch.
    if not await _notifications(factory):
        await _make_pending_jobs_due(factory)
        assert await worker.run_once() == 1
    notifications = await _notifications(factory)
    assert len(notifications) == 1
    assert notifications[0].kind == "bootstrap_digest"
    assert notifications[0].status == "pending"
    assert "1 eligible; 1 assessed; 0 unassessed" in str(notifications[0].payload["message"])

    discovered = listing_factory(
        source_listing_id="333333333",
        canonical_url="https://www.seloger.com/annonces/333333333",
        title="New hard match",
    )
    source.outcomes.append(_result(baseline, discovered))
    await _enqueue_scan(factory, watch_id)
    assert await worker.run_once() == 1
    assert await worker.run_once() == 1  # Assessment creates delivery job.
    assert await worker.run_once() == 1
    notifications = await _notifications(factory)
    listing_notifications = tuple(item for item in notifications if item.kind == "listing")
    assert len(listing_notifications) == 1
    assert listing_notifications[0].payload["listing_id"] is not None
    assert "New hard match" in str(listing_notifications[0].payload["message"])

    # Simulate a crash after the notification transaction committed but before its job completed.
    async with session_scope(factory) as session:
        sent_job = await session.scalar(select(JobRecord).where(JobRecord.kind == "notification"))
        assert sent_job is not None
        sent_job.status = "running"
        sent_job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    restarted_worker = JobWorker(
        session_factory=factory,
        source=source,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
        model_provider="openai",
        model_name="test-model",
        tracing=tracing,  # type: ignore[arg-type]
    )
    assert await restarted_worker.run_once() == 1
    assert len(await _notifications(factory)) == 2

    # Semantic changes are versioned but never produce a V0 alert.
    price_changed = listing_factory(
        source_listing_id="333333333",
        canonical_url="https://www.seloger.com/annonces/333333333",
        title="New hard match",
        rent_eur_monthly=1_650,
    )
    source.outcomes.append(_result(baseline, price_changed))
    await _enqueue_scan(factory, watch_id)
    assert await restarted_worker.run_once() == 1
    assert await restarted_worker.run_once() == 0
    assert len(await _notifications(factory)) == 2

    # Two bounded source failures fail the scan without changing presence state.
    source.outcomes.extend([SourceBlockedError("blocked"), SourceBlockedError("blocked again")])
    await _enqueue_scan(factory, watch_id)
    assert await restarted_worker.run_once() == 1
    async with session_scope(factory) as session:
        current = await session.scalar(
            select(ListingRecord).where(ListingRecord.source_listing_id == "333333333")
        )
        failed_scan = await session.scalar(
            select(ScanRunRecord)
            .where(ScanRunRecord.status == "failed")
            .order_by(ScanRunRecord.started_at.desc())
        )
        assert current is not None and current.active and current.missing_streak == 0
        assert failed_scan is not None and failed_scan.error_code == "source_blocked"

    # Model exhaustion still creates a deterministic analysis-unavailable alert.
    agent.fail = True
    fallback = listing_factory(
        source_listing_id="444444444",
        canonical_url="https://www.seloger.com/annonces/444444444",
        title="Fallback alert listing",
    )
    source.outcomes.append(_result(baseline, price_changed, fallback))
    await _enqueue_scan(factory, watch_id)
    assert await restarted_worker.run_once() == 1
    assert await restarted_worker.run_once() == 1
    await _make_pending_jobs_due(factory)
    assert await restarted_worker.run_once() == 1
    assert await restarted_worker.run_once() == 1
    notifications = await _notifications(factory)
    assert len(notifications) == 3
    assert "Fallback alert listing" in str(notifications[-1].payload["message"])
    assert "Analysis unavailable" in str(notifications[-1].payload["message"])

    async with session_scope(factory) as session:
        notifications = tuple((await session.scalars(select(NotificationRecord))).all())
        assert all(notification.status == "pending" for notification in notifications)
