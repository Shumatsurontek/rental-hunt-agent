from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rental_hunt import repository
from rental_hunt.bounds import BOUNDS
from rental_hunt.contracts import (
    AssessmentJobPayload,
    BrowserNotificationPayload,
    NormalizedListing,
    NotificationJobPayload,
    PreferencesUpdate,
    ScanResult,
    WatchConfigurationUpdate,
    WatchCreate,
)
from rental_hunt.database import session_scope
from rental_hunt.models import (
    EventRecord,
    FeedbackRecord,
    JobRecord,
    ListingRecord,
    ListingVersionRecord,
    NotificationRecord,
)
from rental_hunt.repository import (
    ConflictError,
    QueueFullError,
    acknowledge_notification,
    claim_jobs,
    create_browser_notification,
    create_watch,
    enqueue_job,
    get_assessment_context,
    get_notification_context,
    list_listings,
    list_pending_notifications,
    persist_scan_result,
    put_preferences,
    record_feedback,
    start_scan_run,
    update_watch_configuration,
)


async def _setup_watch(
    factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_scope(factory) as session:
        await put_preferences(
            session,
            PreferencesUpdate(
                rent_eur_monthly_max=2_000,
                surface_m2_min=Decimal("30"),
                rooms_min=2,
                furnished="any",
                postal_codes_allowed=("75011",),
                soft_preferences=("quiet",),
            ),
        )
        watch = await create_watch(
            session,
            WatchCreate(url="https://www.seloger.com/list.htm", poll_interval_s=600),
        )
        return watch.id


async def _persist(
    factory: async_sessionmaker[AsyncSession],
    watch_id: uuid.UUID,
    listings: tuple[NormalizedListing, ...],
    *,
    watch_configuration_version: int = 1,
) -> uuid.UUID:
    async with session_scope(factory) as session:
        scan = await start_scan_run(
            session,
            job_id=uuid.uuid4(),
            watch_id=watch_id,
        )
        await persist_scan_result(
            session,
            scan_id=scan.id,
            watch_id=watch_id,
            watch_configuration_version=watch_configuration_version,
            result=ScanResult(
                listings=listings,
                observed_at=datetime.now(UTC),
                source_total=len(listings),
                pages_scanned=1,
            ),
        )
        return scan.id


@pytest.mark.asyncio
async def test_watch_configuration_edits_are_atomic_versioned_and_preserve_history(
    database: tuple[object, async_sessionmaker[AsyncSession]],
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    _, factory = database
    watch_id = await _setup_watch(factory)
    listing = listing_factory()
    await _persist(factory, watch_id, (listing,))
    changed_preferences = PreferencesUpdate(
        rent_eur_monthly_max=2_300,
        surface_m2_min=Decimal("35"),
        rooms_min=2,
        furnished="required",
        postal_codes_allowed=("75011", "75012"),
        soft_preferences=("quiet street",),
    )

    async with session_scope(factory) as session:
        with pytest.raises(ConflictError, match="active jobs"):
            await update_watch_configuration(
                session,
                watch_id,
                WatchConfigurationUpdate(
                    watch=WatchCreate(
                        url="https://www.seloger.com/list.htm?projects=2",
                        poll_interval_s=900,
                    ),
                    preferences=changed_preferences,
                ),
            )

    async with session_scope(factory) as session:
        for job in (await session.scalars(select(JobRecord))).all():
            job.status = "succeeded"
        await session.flush()
        original_preferences = PreferencesUpdate(
            rent_eur_monthly_max=2_000,
            surface_m2_min=Decimal("30"),
            rooms_min=2,
            furnished="any",
            postal_codes_allowed=("75011",),
            soft_preferences=("quiet",),
        )
        frequency_only, _ = await update_watch_configuration(
            session,
            watch_id,
            WatchConfigurationUpdate(
                watch=WatchCreate(
                    url="https://www.seloger.com/list.htm",
                    poll_interval_s=900,
                ),
                preferences=original_preferences,
            ),
        )
        assert frequency_only.configuration_version == 1
        assert frequency_only.baseline_complete

        revised, saved_preferences = await update_watch_configuration(
            session,
            watch_id,
            WatchConfigurationUpdate(
                watch=WatchCreate(
                    url="https://www.seloger.com/list.htm?projects=2",
                    poll_interval_s=900,
                ),
                preferences=changed_preferences,
            ),
        )
        assert revised.configuration_version == 2
        assert not revised.baseline_complete
        assert saved_preferences == changed_preferences
        assert (await list_listings(session, cursor=None, limit=10)).items == ()

    await _persist(
        factory,
        watch_id,
        (listing,),
        watch_configuration_version=2,
    )

    async with session_scope(factory) as session:
        records = tuple(
            (
                await session.scalars(
                    select(ListingRecord).order_by(ListingRecord.configuration_version)
                )
            ).all()
        )
        events = tuple(
            (
                await session.scalars(
                    select(EventRecord)
                    .where(EventRecord.kind == "watch.configuration_updated")
                    .order_by(EventRecord.occurred_at)
                )
            ).all()
        )
        visible = await list_listings(session, cursor=None, limit=10)
        assert [record.configuration_version for record in records] == [1, 2]
        assert [item.id for item in visible.items] == [records[1].id]
        assert len(events) == 2
        material_event = events[-1].payload
        assert material_event["previous"]["version"] == 1
        assert material_event["previous"]["preferences"]["rent_eur_monthly_max"] == 2_000
        assert material_event["current"]["version"] == 2
        assert material_event["current"]["preferences"]["rent_eur_monthly_max"] == 2_300


@pytest.mark.asyncio
async def test_bootstrap_then_new_listing_and_price_change_are_idempotent(
    database: tuple[object, async_sessionmaker[AsyncSession]],
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    _, factory = database
    watch_id = await _setup_watch(factory)
    first = listing_factory()
    rejected = listing_factory(
        source_listing_id="222222222",
        canonical_url="https://www.seloger.com/annonces/222222222",
        rent_eur_monthly=2_001,
    )

    await _persist(factory, watch_id, (first, rejected))
    async with session_scope(factory) as session:
        eligible_page = await list_listings(
            session,
            cursor=None,
            limit=100,
            eligible_only=True,
        )
        assert [item.listing.source_listing_id for item in eligible_page.items] == [
            first.source_listing_id
        ]
        jobs = tuple((await session.scalars(select(JobRecord).order_by(JobRecord.kind))).all())
        assert [job.kind for job in jobs] == ["assessment", "bootstrap_digest"]
        bootstrap_assessment = next(job for job in jobs if job.kind == "assessment")
        assert bootstrap_assessment.payload["notify"] is False

    second = listing_factory(
        source_listing_id="333333333",
        canonical_url="https://www.seloger.com/annonces/333333333",
        title="Nouveau trois pièces",
    )
    await _persist(factory, watch_id, (first, rejected, second))
    async with session_scope(factory) as session:
        assessments = tuple(
            (await session.scalars(select(JobRecord).where(JobRecord.kind == "assessment"))).all()
        )
        assert len(assessments) == 2
        new_job = next(job for job in assessments if job.id != bootstrap_assessment.id)
        assert new_job.payload["notify"] is True
        listing_id = uuid.UUID(new_job.payload["listing_id"])
        discovery_fingerprint = str(new_job.payload["fingerprint"])

    changed = listing_factory(
        source_listing_id="333333333",
        canonical_url="https://www.seloger.com/annonces/333333333",
        title="Nouveau trois pièces",
        rent_eur_monthly=1_600,
    )
    await _persist(factory, watch_id, (first, rejected, changed))
    await _persist(factory, watch_id, (first, rejected, changed))

    async with session_scope(factory) as session:
        assessment_count = await session.scalar(
            select(func.count(JobRecord.id)).where(JobRecord.kind == "assessment")
        )
        version_count = await session.scalar(
            select(func.count(ListingVersionRecord.id)).where(
                ListingVersionRecord.listing_id == listing_id
            )
        )
        changed_event_count = await session.scalar(
            select(func.count(EventRecord.id)).where(EventRecord.kind == "listing.changed")
        )
        assert assessment_count == 2
        assert version_count == 2
        assert changed_event_count == 1

        # Queued work resolves the discovery version even after the current price changed.
        context = await get_assessment_context(
            session,
            AssessmentJobPayload(
                listing_id=listing_id,
                fingerprint=discovery_fingerprint,
                notify=True,
            ),
        )
        assert context.listing.rent_eur_monthly == 1_500
        notification = await get_notification_context(
            session,
            NotificationJobPayload(
                listing_id=listing_id,
                fingerprint=discovery_fingerprint,
            ),
        )
        assert notification.listing.rent_eur_monthly == 1_500


@pytest.mark.asyncio
async def test_three_complete_absences_deactivate_and_reappearance_reuses_row(
    database: tuple[object, async_sessionmaker[AsyncSession]],
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    _, factory = database
    watch_id = await _setup_watch(factory)
    listing = listing_factory()
    await _persist(factory, watch_id, (listing,))

    for _ in range(3):
        await _persist(factory, watch_id, ())

    async with session_scope(factory) as session:
        record = await session.scalar(select(ListingRecord))
        assert record is not None
        original_id = record.id
        assert not record.active
        assert record.missing_streak == 3

    await _persist(factory, watch_id, (listing,))
    async with session_scope(factory) as session:
        records = tuple((await session.scalars(select(ListingRecord))).all())
        assessment_count = await session.scalar(
            select(func.count(JobRecord.id)).where(JobRecord.kind == "assessment")
        )
        assert len(records) == 1
        assert records[0].id == original_id
        assert records[0].active
        assert records[0].missing_streak == 0
        assert assessment_count == 1  # Bootstrap only; reappearance is not a discovery.


@pytest.mark.asyncio
async def test_version_pruning_preserves_fingerprint_needed_by_active_job(
    database: tuple[object, async_sessionmaker[AsyncSession]],
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    _, factory = database
    watch_id = await _setup_watch(factory)
    original = listing_factory()
    await _persist(factory, watch_id, (original,))
    async with session_scope(factory) as session:
        listing_record = await session.scalar(select(ListingRecord))
        assert listing_record is not None
        listing_id = listing_record.id

    for index in range(1, BOUNDS.listing_versions_max + 1):
        changed = listing_factory(
            rent_eur_monthly=1_500 + index,
            observed_at=original.observed_at + timedelta(minutes=index),
        )
        await _persist(factory, watch_id, (changed,))

    async with session_scope(factory) as session:
        versions = tuple(
            (
                await session.scalars(
                    select(ListingVersionRecord).where(
                        ListingVersionRecord.listing_id == listing_id
                    )
                )
            ).all()
        )
        context = await get_assessment_context(
            session,
            AssessmentJobPayload(
                listing_id=listing_id,
                fingerprint=original.fingerprint,
                notify=False,
            ),
        )
        assert len(versions) == BOUNDS.listing_versions_max
        assert context.listing.fingerprint == original.fingerprint


@pytest.mark.asyncio
async def test_queue_capacity_and_expired_lease_recovery(
    database: tuple[object, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = database
    monkeypatch.setattr(repository, "BOUNDS", replace(BOUNDS, job_queue_max=2))
    async with session_scope(factory) as session:
        first = await enqueue_job(
            session,
            kind="scan",
            resource_key="one",
            idempotency_key="one",
            payload={},
        )
        await enqueue_job(
            session,
            kind="scan",
            resource_key="two",
            idempotency_key="two",
            payload={},
        )
        with pytest.raises(QueueFullError):
            await enqueue_job(
                session,
                kind="scan",
                resource_key="three",
                idempotency_key="three",
                payload={},
            )

    async with session_scope(factory) as session:
        first_record = await session.get(JobRecord, first.id, with_for_update=True)
        assert first_record is not None
        first_record.status = "running"
        first_record.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    async with session_scope(factory) as session:
        claimed = await claim_jobs(session, trace_jobs=True)
        recovered = next(job for job in claimed if job.id == first.id)
        assert recovered.status == "running"
        assert recovered.attempts == 1
        assert recovered.trace_id is not None
        assert len(claimed) <= BOUNDS.job_claim_max


@pytest.mark.asyncio
async def test_notification_and_feedback_idempotency(
    database: tuple[object, async_sessionmaker[AsyncSession]],
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    _, factory = database
    watch_id = await _setup_watch(factory)
    await _persist(factory, watch_id, (listing_factory(),))
    async with session_scope(factory) as session:
        listing = await session.scalar(select(ListingRecord))
        assert listing is not None
        notification, created = await create_browser_notification(
            session,
            idempotency_key="listing:test",
            kind="listing",
            listing_id=listing.id,
            payload=BrowserNotificationPayload(
                title="New hard match",
                message="A documented match.",
                listing_id=listing.id,
                listing_url=listing.canonical_url,
            ),
        )
        listing_id = listing.id

    async with session_scope(factory) as session:
        duplicate, created_again = await create_browser_notification(
            session,
            idempotency_key="listing:test",
            kind="listing",
            listing_id=listing_id,
            payload=BrowserNotificationPayload(
                title="New hard match",
                message="A documented match.",
                listing_id=listing_id,
                listing_url="https://www.seloger.com/annonces/123456789",
            ),
        )
        page = await list_pending_notifications(session, limit=20)
        assert created
        assert not created_again
        assert duplicate.id == notification.id
        assert [item.id for item in page.items] == [notification.id]
        acknowledged = await acknowledge_notification(session, notification.id)
        assert acknowledged.status == "sent"
        assert acknowledged.delivered_at is not None
        assert (await acknowledge_notification(session, notification.id)).id == notification.id
        await record_feedback(
            session,
            event_id="chrome:event-1",
            listing_id=listing_id,
            value="interested",
        )
        await record_feedback(
            session,
            event_id="chrome:event-1",
            listing_id=listing_id,
            value="interested",
        )
        await record_feedback(
            session,
            event_id="chrome:event-2",
            listing_id=listing_id,
            value="dismissed",
        )

    async with session_scope(factory) as session:
        feedback = await session.scalar(select(FeedbackRecord))
        event_count = await session.scalar(
            select(func.count(EventRecord.id)).where(EventRecord.kind.like("user.%"))
        )
        notification_count = await session.scalar(select(func.count(NotificationRecord.id)))
        assert feedback is not None
        assert feedback.value == "dismissed"
        assert feedback.actor == "chrome_extension"
        assert feedback.notification_id == notification.id
        assert event_count == 2
        assert notification_count == 1
