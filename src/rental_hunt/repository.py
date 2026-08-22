"""Transactional persistence operations and the durable leased job queue."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from rental_hunt.bounds import BOUNDS
from rental_hunt.capture_metadata import browser_capture_metadata
from rental_hunt.contracts import (
    AssessmentJobPayload,
    BootstrapDigestJobPayload,
    BrowserNotificationPage,
    BrowserNotificationPayload,
    BrowserNotificationView,
    BrowserScanCapture,
    Eligibility,
    FeedbackValue,
    FurnishedPreference,
    JobView,
    ListingAssessment,
    ListingPage,
    ListingView,
    NormalizedListing,
    NotificationJobPayload,
    NotificationKind,
    PreferencesUpdate,
    ScanJobPayload,
    ScanResult,
    WatchConfigurationUpdate,
    WatchCreate,
)
from rental_hunt.models import (
    AssessmentRecord,
    EventRecord,
    FeedbackRecord,
    JobRecord,
    ListingRecord,
    ListingVersionRecord,
    NotificationRecord,
    PreferenceRecord,
    ScanRunRecord,
    WatchRecord,
)
from rental_hunt.policy import evaluate_listing

logger = logging.getLogger(__name__)

JobKind = Literal["scan", "assessment", "notification", "bootstrap_digest"]
JobStatus = Literal["pending", "running", "succeeded", "dead"]
ACTIVE_JOB_STATUSES = ("pending", "running")


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class ConflictError(RepositoryError):
    pass


class QueueFullError(RepositoryError):
    pass


class MissingPreferencesError(RepositoryError):
    pass


@dataclass(frozen=True)
class ScanPersistOutcome:
    scan_id: uuid.UUID
    discovered_count: int
    changed_count: int
    eligible_new_count: int
    versions_pruned_count: int


@dataclass(frozen=True)
class AssessmentContext:
    listing: NormalizedListing
    preferences: PreferencesUpdate


@dataclass(frozen=True)
class DigestEntry:
    listing_id: uuid.UUID
    listing: NormalizedListing
    assessment: ListingAssessment | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def put_preferences(
    session: AsyncSession,
    preferences: PreferencesUpdate,
) -> PreferenceRecord:
    record = await session.get(PreferenceRecord, 1, with_for_update=True)
    now = _utc_now()
    if record is None:
        record = PreferenceRecord(id=1)
        session.add(record)
        record.version = 1
    else:
        record.version += 1
    record.rent_eur_monthly_max = preferences.rent_eur_monthly_max
    record.surface_m2_min = preferences.surface_m2_min
    record.rooms_min = preferences.rooms_min
    record.furnished = preferences.furnished
    record.postal_codes_allowed = list(preferences.postal_codes_allowed)
    record.soft_preferences = list(preferences.soft_preferences)
    record.updated_at = now
    await session.flush()
    return record


async def get_preferences(session: AsyncSession) -> PreferencesUpdate | None:
    record = await session.get(PreferenceRecord, 1)
    if record is None:
        return None
    return PreferencesUpdate(
        rent_eur_monthly_max=record.rent_eur_monthly_max,
        surface_m2_min=record.surface_m2_min,
        rooms_min=record.rooms_min,
        furnished=cast(FurnishedPreference, record.furnished),
        postal_codes_allowed=tuple(record.postal_codes_allowed),
        soft_preferences=tuple(record.soft_preferences),
    )


async def create_watch(session: AsyncSession, request: WatchCreate) -> WatchRecord:
    active = await _get_enabled_watch(session, for_update=True)
    if active is not None:
        raise ConflictError("an active watch already exists")
    now = _utc_now()
    record = WatchRecord(
        url=str(request.url),
        poll_interval_s=request.poll_interval_s,
        configuration_version=1,
        enabled=True,
        baseline_complete=False,
        next_scan_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    await session.flush()
    return record


async def get_current_watch(session: AsyncSession) -> WatchRecord | None:
    return await _get_enabled_watch(session, for_update=False)


async def get_watch(session: AsyncSession, watch_id: uuid.UUID) -> WatchRecord:
    watch = await session.get(WatchRecord, watch_id)
    if watch is None:
        raise NotFoundError("watch not found")
    return watch


async def set_watch_enabled(
    session: AsyncSession,
    watch_id: uuid.UUID,
    *,
    enabled: bool,
) -> WatchRecord:
    watch = await session.get(WatchRecord, watch_id, with_for_update=True)
    if watch is None:
        raise NotFoundError("watch not found")
    if enabled:
        active = await _get_enabled_watch(session, for_update=True)
        if active is not None and active.id != watch_id:
            raise ConflictError("another active watch already exists")
        watch.next_scan_at = _utc_now()
    watch.enabled = enabled
    watch.updated_at = _utc_now()
    await session.flush()
    return watch


async def update_watch_configuration(
    session: AsyncSession,
    watch_id: uuid.UUID,
    configuration: WatchConfigurationUpdate,
) -> tuple[WatchRecord, PreferencesUpdate]:
    watch = await session.get(WatchRecord, watch_id, with_for_update=True)
    if watch is None:
        raise NotFoundError("watch not found")
    if not watch.enabled:
        raise ConflictError("only the active watch can be edited")
    current_preferences = await get_preferences(session)
    if current_preferences is None:
        raise MissingPreferencesError("preferences are not configured")

    requested_url = str(configuration.watch.url)
    url_changed = watch.url != requested_url
    poll_changed = watch.poll_interval_s != configuration.watch.poll_interval_s
    preferences_changed = current_preferences != configuration.preferences
    if not (url_changed or poll_changed or preferences_changed):
        return watch, current_preferences
    if await active_job_count(session) > 0:
        raise ConflictError("wait for active jobs to finish before editing the watch")

    now = _utc_now()
    previous_version = watch.configuration_version
    previous_configuration = {
        "poll_interval_s": watch.poll_interval_s,
        "preferences": current_preferences.model_dump(mode="json"),
        "url": watch.url,
        "version": previous_version,
    }
    preferences = current_preferences
    if url_changed or preferences_changed:
        if previous_version >= BOUNDS.watch_configuration_versions_max:
            raise ConflictError(
                "watch configuration revision limit reached; create a fresh deployment"
            )
        watch.configuration_version += 1
        watch.baseline_complete = False
        await put_preferences(session, configuration.preferences)
        preferences = configuration.preferences

    watch.url = requested_url
    watch.poll_interval_s = configuration.watch.poll_interval_s
    watch.next_scan_at = now
    watch.updated_at = now
    await _append_event(
        session,
        kind="watch.configuration_updated",
        entity_id=str(watch.id),
        dedupe_key=f"watch.configuration_updated:{watch.id}:{watch.configuration_version}:{now.isoformat()}",
        payload={
            "current": {
                "poll_interval_s": watch.poll_interval_s,
                "preferences": preferences.model_dump(mode="json"),
                "url": watch.url,
                "version": watch.configuration_version,
            },
            "preferences_changed": preferences_changed,
            "previous": previous_configuration,
            "url_changed": url_changed,
        },
    )
    await session.flush()
    return watch, preferences


async def enqueue_manual_scan(session: AsyncSession, watch_id: uuid.UUID) -> JobRecord:
    watch = await session.get(WatchRecord, watch_id, with_for_update=True)
    if watch is None:
        raise NotFoundError("watch not found")
    if not watch.enabled:
        raise ConflictError("disabled watches cannot be scanned")
    return await _enqueue_scan(session, watch, trigger="manual")


async def enqueue_browser_scan(
    session: AsyncSession,
    watch_id: uuid.UUID,
    capture: BrowserScanCapture,
) -> JobRecord:
    watch = await session.get(WatchRecord, watch_id, with_for_update=True)
    if watch is None:
        raise NotFoundError("watch not found")
    if not watch.enabled:
        raise ConflictError("disabled watches cannot be scanned")
    if capture.watch_configuration_version != watch.configuration_version:
        raise ConflictError("browser capture belongs to an outdated watch configuration")
    idempotency_key = f"browser-scan:{watch.id}:{capture.capture_id}"
    existing = await session.scalar(
        select(JobRecord).where(JobRecord.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    resource_key = _watch_key(watch.id)
    active = await _active_job_for_resource(session, kind="scan", resource_key=resource_key)
    if active is not None:
        raise ConflictError("a scan is already pending or running")
    payload = ScanJobPayload(
        watch_id=watch.id,
        watch_configuration_version=watch.configuration_version,
        trigger="browser",
        browser_capture=capture,
    )
    return await enqueue_job(
        session,
        kind="scan",
        resource_key=resource_key,
        idempotency_key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )


async def enqueue_due_scan(session: AsyncSession) -> JobRecord | None:
    if await get_preferences(session) is None:
        return None
    now = _utc_now()
    statement = (
        select(WatchRecord)
        .where(WatchRecord.enabled.is_(True), WatchRecord.next_scan_at <= now)
        .order_by(WatchRecord.next_scan_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    watch = await session.scalar(statement)
    if watch is None:
        return None
    active_scan = await _active_job_for_resource(
        session, kind="scan", resource_key=_watch_key(watch.id)
    )
    if active_scan is not None:
        return None
    job = await _enqueue_scan(session, watch, trigger="scheduled")
    watch.next_scan_at = now + timedelta(seconds=watch.poll_interval_s)
    watch.updated_at = now
    return job


async def _enqueue_scan(
    session: AsyncSession,
    watch: WatchRecord,
    *,
    trigger: Literal["manual", "scheduled"],
) -> JobRecord:
    resource_key = _watch_key(watch.id)
    active = await _active_job_for_resource(session, kind="scan", resource_key=resource_key)
    if active is not None:
        raise ConflictError("a scan is already pending or running")
    payload = ScanJobPayload(
        watch_id=watch.id,
        watch_configuration_version=watch.configuration_version,
        trigger=trigger,
    )
    return await enqueue_job(
        session,
        kind="scan",
        resource_key=resource_key,
        idempotency_key=f"scan:{watch.id}:{uuid.uuid4()}",
        payload=payload.model_dump(mode="json"),
    )


async def start_scan_run(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    watch_id: uuid.UUID,
) -> ScanRunRecord:
    existing = await session.scalar(select(ScanRunRecord).where(ScanRunRecord.job_id == job_id))
    if existing is not None:
        return existing
    record = ScanRunRecord(job_id=job_id, watch_id=watch_id, status="running")
    session.add(record)
    await session.flush()
    return record


async def fail_scan_run(
    session: AsyncSession,
    scan_id: uuid.UUID,
    *,
    error_code: str,
    error_detail: str,
) -> None:
    scan = await session.get(ScanRunRecord, scan_id, with_for_update=True)
    if scan is None:
        raise NotFoundError("scan run not found")
    scan.status = "failed"
    scan.error_code = error_code[:64]
    scan.error_detail = error_detail[:4_000]
    scan.completed_at = _utc_now()


async def persist_scan_result(  # noqa: PLR0912, PLR0913 - atomic scan invariants are explicit.
    session: AsyncSession,
    *,
    scan_id: uuid.UUID,
    watch_id: uuid.UUID,
    watch_configuration_version: int,
    result: ScanResult,
    source_url: str | None = None,
) -> ScanPersistOutcome:
    watch = await session.get(WatchRecord, watch_id, with_for_update=True)
    if watch is None:
        raise NotFoundError("watch not found")
    if watch.configuration_version != watch_configuration_version:
        raise ConflictError("scan belongs to an outdated watch configuration")
    scan = await session.get(ScanRunRecord, scan_id, with_for_update=True)
    if scan is None:
        raise NotFoundError("scan run not found")
    preferences = await get_preferences(session)
    if preferences is None:
        raise MissingPreferencesError("preferences must be configured before scanning")

    active_records = await _active_listings(session, watch_id, watch_configuration_version)
    existing_by_source_id = await _listings_by_source_id(
        session,
        watch_id,
        watch_configuration_version,
        tuple(listing.source_listing_id for listing in result.listings),
    )
    new_records: list[ListingRecord] = []
    changed_count = 0
    pruned_count = 0
    seen_ids: set[uuid.UUID] = set()

    for listing in result.listings:
        record = existing_by_source_id.get(listing.source_listing_id)
        eligibility = evaluate_listing(listing, preferences)
        if record is None:
            record = await _insert_listing(
                session,
                watch_id,
                watch_configuration_version,
                listing,
                eligibility,
                scan_id,
            )
            new_records.append(record)
        elif record.fingerprint != listing.fingerprint:
            await _change_listing(session, record, listing, eligibility, scan_id)
            await session.flush()
            changed_count += 1
            pruned_count += await _prune_listing_versions(session, record.id)
        else:
            _refresh_listing(record, listing, eligibility)
        seen_ids.add(record.id)

    await _mark_missing_listings(session, active_records, seen_ids, scan_id)
    eligible_new = [record for record in new_records if bool(record.eligibility["eligible"])]
    jobs_needed = len(eligible_new)
    if not watch.baseline_complete:
        jobs_needed = min(jobs_needed, BOUNDS.bootstrap_assessments_max) + 1
    await ensure_queue_capacity(session, jobs_needed)

    if watch.baseline_complete:
        for record in eligible_new:
            await _enqueue_assessment(session, record, notify=True, bootstrap_scan_id=None)
    else:
        selected = _select_bootstrap_records(eligible_new)
        for record in selected:
            await _enqueue_assessment(
                session,
                record,
                notify=False,
                bootstrap_scan_id=scan_id,
                capacity_checked=True,
            )
        await _enqueue_bootstrap_digest(
            session,
            scan_id=scan_id,
            selected=selected,
            eligible_total=len(eligible_new),
            capacity_checked=True,
        )
        watch.baseline_complete = True

    scan.status = "succeeded"
    scan.listing_count = len(result.listings)
    scan.discovered_count = len(new_records)
    scan.changed_count = changed_count
    scan.completed_at = _utc_now()
    _update_watch_after_browser_scan(watch, source_url)
    return ScanPersistOutcome(
        scan_id=scan_id,
        discovered_count=len(new_records),
        changed_count=changed_count,
        eligible_new_count=len(eligible_new),
        versions_pruned_count=pruned_count,
    )


async def enqueue_job(  # noqa: PLR0913 - durable job identity is intentionally explicit.
    session: AsyncSession,
    *,
    kind: JobKind,
    resource_key: str,
    idempotency_key: str,
    payload: dict[str, Any],
    group_key: str | None = None,
    capacity_checked: bool = False,
) -> JobRecord:
    if not capacity_checked:
        await _lock_job_queue(session)
    existing = await session.scalar(
        select(JobRecord).where(JobRecord.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    if not capacity_checked:
        await _assert_queue_capacity(session, 1)
    job = JobRecord(
        kind=kind,
        status="pending",
        idempotency_key=idempotency_key,
        resource_key=resource_key,
        group_key=group_key,
        payload=payload,
        attempts=0,
        available_at=_utc_now(),
    )
    session.add(job)
    await session.flush()
    return job


async def ensure_queue_capacity(session: AsyncSession, requested: int) -> None:
    if requested < 0:
        raise AssertionError("requested queue capacity cannot be negative")
    await _lock_job_queue(session)
    await _assert_queue_capacity(session, requested)


async def _lock_job_queue(session: AsyncSession) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        # Serialize capacity checks across worker transactions; no job may be dropped.
        await session.execute(text("SELECT pg_advisory_xact_lock(1919241541)"))


async def _lock_notification_capacity(session: AsyncSession) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        # A separate lock keeps outbox capacity exact without blocking job enqueueing.
        await session.execute(text("SELECT pg_advisory_xact_lock(1919241542)"))


async def _assert_queue_capacity(session: AsyncSession, requested: int) -> None:
    active_count = await session.scalar(
        select(func.count(JobRecord.id)).where(JobRecord.status.in_(ACTIVE_JOB_STATUSES))
    )
    count = int(active_count or 0)
    if count + requested > BOUNDS.job_queue_max:
        raise QueueFullError(f"durable job queue would exceed {BOUNDS.job_queue_max} active jobs")


async def claim_jobs(
    session: AsyncSession,
    *,
    trace_jobs: bool = False,
) -> tuple[JobRecord, ...]:
    now = _utc_now()
    claimable = or_(
        and_(JobRecord.status == "pending", JobRecord.available_at <= now),
        and_(JobRecord.status == "running", JobRecord.lease_until < now),
    )
    statement = (
        select(JobRecord)
        .where(claimable)
        .order_by(JobRecord.available_at, JobRecord.created_at)
        .with_for_update(skip_locked=True)
        .limit(BOUNDS.job_claim_max)
    )
    jobs = tuple((await session.scalars(statement)).all())
    lease_until = now + timedelta(seconds=BOUNDS.job_lease_s)
    for job in jobs:
        job.status = "running"
        job.attempts += 1
        job.trace_id = uuid.uuid4() if trace_jobs else None
        job.lease_until = lease_until
        job.updated_at = now
    await session.flush()
    return jobs


async def complete_job(session: AsyncSession, job_id: uuid.UUID) -> None:
    job = await _locked_job(session, job_id)
    job.status = "succeeded"
    job.lease_until = None
    job.last_error = None
    job.updated_at = _utc_now()
    _compact_browser_scan_payload(job)


async def defer_job(session: AsyncSession, job_id: uuid.UUID, *, delay_s: int) -> None:
    job = await _locked_job(session, job_id)
    job.status = "pending"
    job.attempts = max(job.attempts - 1, 0)
    job.available_at = _utc_now() + timedelta(seconds=delay_s)
    job.lease_until = None
    job.updated_at = _utc_now()


async def fail_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    error: str,
    attempts_max: int,
) -> bool:
    if attempts_max < 1:
        raise AssertionError("attempts_max must be positive")
    job = await _locked_job(session, job_id)
    terminal = job.attempts >= attempts_max
    job.last_error = error[:4_000]
    job.lease_until = None
    job.updated_at = _utc_now()
    if terminal:
        job.status = "dead"
        _compact_browser_scan_payload(job)
        if job.kind == "assessment":
            payload = AssessmentJobPayload.model_validate(job.payload)
            if payload.notify:
                await _enqueue_notification(session, payload, capacity_checked=False)
    else:
        delay_s = min(5 * (2 ** (job.attempts - 1)), 300)
        job.status = "pending"
        job.available_at = _utc_now() + timedelta(seconds=delay_s)
    return terminal


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> JobView:
    job = await session.get(JobRecord, job_id)
    if job is None:
        raise NotFoundError("job not found")
    return JobView.model_validate(job)


async def active_job_count(session: AsyncSession) -> int:
    value = await session.scalar(
        select(func.count(JobRecord.id)).where(JobRecord.status.in_(ACTIVE_JOB_STATUSES))
    )
    return int(value or 0)


async def get_assessment_context(
    session: AsyncSession,
    payload: AssessmentJobPayload,
) -> AssessmentContext:
    listing_record = await session.get(ListingRecord, payload.listing_id)
    if listing_record is None:
        raise NotFoundError("listing not found")
    listing_payload = await _listing_payload_for_fingerprint(
        session,
        listing_record,
        payload.fingerprint,
    )
    preferences = await get_preferences(session)
    if preferences is None:
        raise MissingPreferencesError("preferences are not configured")
    return AssessmentContext(
        listing=NormalizedListing.model_validate(listing_payload),
        preferences=preferences,
    )


async def save_assessment(
    session: AsyncSession,
    *,
    payload: AssessmentJobPayload,
    assessment: ListingAssessment,
    model_provider: str,
    model_name: str,
) -> AssessmentRecord:
    statement = select(AssessmentRecord).where(
        AssessmentRecord.listing_id == payload.listing_id,
        AssessmentRecord.fingerprint == payload.fingerprint,
    )
    record = await session.scalar(statement)
    if record is None:
        record = AssessmentRecord(
            listing_id=payload.listing_id,
            fingerprint=payload.fingerprint,
            model_provider=model_provider,
            model_name=model_name,
            payload=assessment.model_dump(mode="json"),
        )
        session.add(record)
        await session.flush()
    if payload.notify:
        await _enqueue_notification(session, payload, capacity_checked=False)
    return record


async def bootstrap_assessments_pending(session: AsyncSession, scan_id: uuid.UUID) -> bool:
    group_key = _bootstrap_key(scan_id)
    count = await session.scalar(
        select(func.count(JobRecord.id)).where(
            JobRecord.kind == "assessment",
            JobRecord.group_key == group_key,
            JobRecord.status.in_(ACTIVE_JOB_STATUSES),
        )
    )
    return bool(count)


async def get_digest_entries(
    session: AsyncSession,
    payload: BootstrapDigestJobPayload,
) -> tuple[DigestEntry, ...]:
    entries: list[DigestEntry] = []
    for listing_id in payload.listing_ids:
        listing_record = await session.get(ListingRecord, listing_id)
        if listing_record is None:
            continue
        assessment_record = await session.scalar(
            select(AssessmentRecord).where(
                AssessmentRecord.listing_id == listing_id,
                AssessmentRecord.fingerprint == listing_record.fingerprint,
            )
        )
        assessment = (
            ListingAssessment.model_validate(assessment_record.payload)
            if assessment_record is not None
            else None
        )
        entries.append(
            DigestEntry(
                listing_id=listing_id,
                listing=NormalizedListing.model_validate(listing_record.payload),
                assessment=assessment,
            )
        )
    return tuple(entries)


async def get_notification_context(
    session: AsyncSession,
    payload: NotificationJobPayload,
) -> DigestEntry:
    listing_record = await session.get(ListingRecord, payload.listing_id)
    if listing_record is None:
        raise NotFoundError("listing not found")
    listing_payload = await _listing_payload_for_fingerprint(
        session,
        listing_record,
        payload.fingerprint,
    )
    assessment_record = await session.scalar(
        select(AssessmentRecord).where(
            AssessmentRecord.listing_id == payload.listing_id,
            AssessmentRecord.fingerprint == payload.fingerprint,
        )
    )
    assessment = (
        ListingAssessment.model_validate(assessment_record.payload)
        if assessment_record is not None
        else None
    )
    return DigestEntry(
        listing_id=listing_record.id,
        listing=NormalizedListing.model_validate(listing_payload),
        assessment=assessment,
    )


async def create_browser_notification(
    session: AsyncSession,
    *,
    idempotency_key: str,
    kind: NotificationKind,
    listing_id: uuid.UUID | None,
    payload: BrowserNotificationPayload,
) -> tuple[NotificationRecord, bool]:
    await _lock_notification_capacity(session)
    statement = (
        select(NotificationRecord)
        .where(NotificationRecord.idempotency_key == idempotency_key)
        .with_for_update()
    )
    record = await session.scalar(statement)
    if record is not None:
        return record, False
    pending = await pending_notification_count(session)
    if pending >= BOUNDS.browser_notifications_pending_max:
        raise QueueFullError(
            "browser notification outbox reached "
            f"{BOUNDS.browser_notifications_pending_max} pending items"
        )
    record = NotificationRecord(
        listing_id=listing_id,
        kind=kind,
        idempotency_key=idempotency_key,
        status="pending",
        payload=payload.model_dump(mode="json"),
    )
    session.add(record)
    await session.flush()
    return record, True


async def pending_notification_count(session: AsyncSession) -> int:
    value = await session.scalar(
        select(func.count(NotificationRecord.id)).where(NotificationRecord.status == "pending")
    )
    return int(value or 0)


async def list_pending_notifications(
    session: AsyncSession,
    *,
    limit: int,
) -> BrowserNotificationPage:
    if limit < 1 or limit > BOUNDS.browser_notifications_pull_max:
        raise ValueError(
            f"notification pull limit must be between 1 and {BOUNDS.browser_notifications_pull_max}"
        )
    records = tuple(
        (
            await session.scalars(
                select(NotificationRecord)
                .where(NotificationRecord.status == "pending")
                .order_by(NotificationRecord.created_at, NotificationRecord.id)
                .limit(limit)
            )
        ).all()
    )
    return BrowserNotificationPage(
        items=tuple(
            BrowserNotificationView(
                id=record.id,
                kind=cast(NotificationKind, record.kind),
                payload=BrowserNotificationPayload.model_validate(record.payload),
                created_at=record.created_at,
            )
            for record in records
        )
    )


async def acknowledge_notification(
    session: AsyncSession,
    notification_id: uuid.UUID,
) -> NotificationRecord:
    record = await session.get(NotificationRecord, notification_id, with_for_update=True)
    if record is None:
        raise NotFoundError("notification not found")
    if record.status == "sent":
        return record
    if record.status != "pending":
        raise ConflictError(f"notification cannot be acknowledged from {record.status}")
    record.status = "sent"
    record.delivered_at = _utc_now()
    record.last_error = None
    return record


async def record_feedback(
    session: AsyncSession,
    *,
    event_id: str,
    listing_id: uuid.UUID,
    value: FeedbackValue,
    actor: str = "chrome_extension",
) -> FeedbackRecord:
    listing = await session.get(ListingRecord, listing_id)
    if listing is None:
        raise NotFoundError("listing not found")
    dedupe_key = f"feedback:{event_id}"
    processed = await session.scalar(
        select(EventRecord).where(EventRecord.dedupe_key == dedupe_key)
    )
    if processed is not None:
        existing_feedback = await session.scalar(
            select(FeedbackRecord).where(FeedbackRecord.listing_id == listing_id)
        )
        if existing_feedback is None:
            raise RepositoryError("feedback event exists without its current-state record")
        return existing_feedback
    notification = await session.scalar(
        select(NotificationRecord)
        .where(NotificationRecord.listing_id == listing_id, NotificationRecord.status == "sent")
        .order_by(NotificationRecord.delivered_at.desc())
        .limit(1)
    )
    feedback = await session.scalar(
        select(FeedbackRecord).where(FeedbackRecord.listing_id == listing_id).with_for_update()
    )
    now = _utc_now()
    if feedback is None:
        feedback = FeedbackRecord(
            listing_id=listing_id,
            notification_id=notification.id if notification is not None else None,
            value=value,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(feedback)
    else:
        feedback.value = value
        feedback.actor = actor
        feedback.notification_id = notification.id if notification is not None else None
        feedback.updated_at = now
    await _append_event(
        session,
        kind=f"user.{value}",
        entity_id=str(listing_id),
        dedupe_key=dedupe_key,
        payload={"actor": actor, "listing_id": str(listing_id), "value": value},
    )
    await session.flush()
    return feedback


async def list_listings(
    session: AsyncSession,
    *,
    cursor: uuid.UUID | None,
    limit: int,
    eligible_only: bool = False,
) -> ListingPage:
    if limit < 1 or limit > 100:
        raise ValueError("listing page limit must be between 1 and 100")
    watch = await _get_enabled_watch(session, for_update=False)
    if watch is None:
        return ListingPage(items=(), next_cursor=None)
    statement = (
        select(ListingRecord)
        .where(
            ListingRecord.watch_id == watch.id,
            ListingRecord.configuration_version == watch.configuration_version,
        )
        .order_by(
            ListingRecord.first_seen_at.desc(),
            ListingRecord.id.desc(),
        )
    )
    if eligible_only:
        statement = statement.where(ListingRecord.eligibility["eligible"].as_boolean().is_(True))
    if cursor is not None:
        cursor_record = await session.get(ListingRecord, cursor)
        if (
            cursor_record is None
            or cursor_record.watch_id != watch.id
            or cursor_record.configuration_version != watch.configuration_version
        ):
            raise NotFoundError("listing cursor not found")
        statement = statement.where(
            or_(
                ListingRecord.first_seen_at < cursor_record.first_seen_at,
                and_(
                    ListingRecord.first_seen_at == cursor_record.first_seen_at,
                    ListingRecord.id < cursor_record.id,
                ),
            )
        )
    records = tuple((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(records) > limit
    visible = records[:limit]
    items = [await _listing_view(session, record) for record in visible]
    next_cursor = visible[-1].id if has_more and visible else None
    return ListingPage(items=tuple(items), next_cursor=next_cursor)


async def cleanup_history(session: AsyncSession) -> tuple[int, int, int]:
    cutoff = _utc_now() - timedelta(days=BOUNDS.scan_history_days)
    scan_ids = tuple(
        (
            await session.scalars(
                select(ScanRunRecord.id)
                .where(ScanRunRecord.completed_at < cutoff)
                .limit(BOUNDS.cleanup_rows_max)
            )
        ).all()
    )
    job_ids = tuple(
        (
            await session.scalars(
                select(JobRecord.id)
                .where(
                    JobRecord.updated_at < cutoff,
                    JobRecord.status.in_(("succeeded", "dead")),
                )
                .limit(BOUNDS.cleanup_rows_max)
            )
        ).all()
    )
    notification_ids = tuple(
        (
            await session.scalars(
                select(NotificationRecord.id)
                .where(
                    NotificationRecord.delivered_at < cutoff,
                    NotificationRecord.status == "sent",
                )
                .limit(BOUNDS.cleanup_rows_max)
            )
        ).all()
    )
    if scan_ids:
        await session.execute(delete(ScanRunRecord).where(ScanRunRecord.id.in_(scan_ids)))
    if job_ids:
        await session.execute(delete(JobRecord).where(JobRecord.id.in_(job_ids)))
    if notification_ids:
        await session.execute(
            delete(NotificationRecord).where(NotificationRecord.id.in_(notification_ids))
        )
    return len(scan_ids), len(job_ids), len(notification_ids)


async def _get_enabled_watch(
    session: AsyncSession,
    *,
    for_update: bool,
) -> WatchRecord | None:
    statement = select(WatchRecord).where(WatchRecord.enabled.is_(True)).limit(1)
    if for_update:
        statement = statement.with_for_update()
    return cast(WatchRecord | None, await session.scalar(statement))


async def _active_job_for_resource(
    session: AsyncSession,
    *,
    kind: JobKind,
    resource_key: str,
) -> JobRecord | None:
    return cast(
        JobRecord | None,
        await session.scalar(
            select(JobRecord)
            .where(
                JobRecord.kind == kind,
                JobRecord.resource_key == resource_key,
                JobRecord.status.in_(ACTIVE_JOB_STATUSES),
            )
            .limit(1)
        ),
    )


async def _active_listings(
    session: AsyncSession,
    watch_id: uuid.UUID,
    watch_configuration_version: int,
) -> tuple[ListingRecord, ...]:
    records = tuple(
        (
            await session.scalars(
                select(ListingRecord)
                .where(
                    ListingRecord.watch_id == watch_id,
                    ListingRecord.configuration_version == watch_configuration_version,
                    ListingRecord.active.is_(True),
                )
                .with_for_update()
                .limit(BOUNDS.active_listings_query_max + 1)
            )
        ).all()
    )
    if len(records) > BOUNDS.active_listings_query_max:
        raise RepositoryError("active listing set exceeds the configured processing bound")
    return records


async def _listings_by_source_id(
    session: AsyncSession,
    watch_id: uuid.UUID,
    watch_configuration_version: int,
    source_ids: tuple[str, ...],
) -> dict[str, ListingRecord]:
    if not source_ids:
        return {}
    if len(source_ids) > BOUNDS.listings_per_scan_max:
        raise AssertionError("normalized scan exceeded the listing bound")
    records = tuple(
        (
            await session.scalars(
                select(ListingRecord)
                .where(
                    ListingRecord.watch_id == watch_id,
                    ListingRecord.configuration_version == watch_configuration_version,
                    ListingRecord.source == "seloger",
                    ListingRecord.source_listing_id.in_(source_ids),
                )
                .with_for_update()
                .limit(BOUNDS.listings_per_scan_max)
            )
        ).all()
    )
    return {record.source_listing_id: record for record in records}


async def _insert_listing(  # noqa: PLR0913, PLR0917 - listing identity is explicit.
    session: AsyncSession,
    watch_id: uuid.UUID,
    watch_configuration_version: int,
    listing: NormalizedListing,
    eligibility: Eligibility,
    scan_id: uuid.UUID,
) -> ListingRecord:
    payload = listing.model_dump(mode="json")
    record = ListingRecord(
        watch_id=watch_id,
        configuration_version=watch_configuration_version,
        source=listing.source,
        source_listing_id=listing.source_listing_id,
        canonical_url=str(listing.canonical_url),
        fingerprint=listing.fingerprint,
        payload=payload,
        eligibility=eligibility.model_dump(mode="json"),
        active=True,
        missing_streak=0,
        first_seen_at=listing.observed_at,
        last_seen_at=listing.observed_at,
    )
    session.add(record)
    await session.flush()
    session.add(
        ListingVersionRecord(
            listing_id=record.id,
            fingerprint=listing.fingerprint,
            payload=payload,
            observed_at=listing.observed_at,
        )
    )
    await _append_event(
        session,
        kind="listing.discovered",
        entity_id=str(record.id),
        dedupe_key=f"listing.discovered:{record.id}:{listing.fingerprint}",
        payload={"listing_id": str(record.id), "scan_id": str(scan_id)},
    )
    return record


async def _change_listing(
    session: AsyncSession,
    record: ListingRecord,
    listing: NormalizedListing,
    eligibility: Eligibility,
    scan_id: uuid.UUID,
) -> None:
    previous_fingerprint = record.fingerprint
    _refresh_listing(record, listing, eligibility)
    session.add(
        ListingVersionRecord(
            listing_id=record.id,
            fingerprint=listing.fingerprint,
            payload=listing.model_dump(mode="json"),
            observed_at=listing.observed_at,
        )
    )
    await _append_event(
        session,
        kind="listing.changed",
        entity_id=str(record.id),
        dedupe_key=f"listing.changed:{record.id}:{listing.fingerprint}",
        payload={
            "listing_id": str(record.id),
            "scan_id": str(scan_id),
            "previous_fingerprint": previous_fingerprint,
        },
    )


def _refresh_listing(
    record: ListingRecord,
    listing: NormalizedListing,
    eligibility: Eligibility,
) -> None:
    record.canonical_url = str(listing.canonical_url)
    record.fingerprint = listing.fingerprint
    record.payload = listing.model_dump(mode="json")
    record.eligibility = eligibility.model_dump(mode="json")
    record.active = True
    record.missing_streak = 0
    record.last_seen_at = listing.observed_at


async def _mark_missing_listings(
    session: AsyncSession,
    existing: tuple[ListingRecord, ...],
    seen_ids: set[uuid.UUID],
    scan_id: uuid.UUID,
) -> None:
    for record in existing:
        if record.id in seen_ids:
            continue
        record.missing_streak += 1
        if record.missing_streak == 3:
            record.active = False
            await _append_event(
                session,
                kind="listing.deactivated",
                entity_id=str(record.id),
                dedupe_key=f"listing.deactivated:{record.id}:{scan_id}",
                payload={"listing_id": str(record.id), "scan_id": str(scan_id)},
            )


async def _prune_listing_versions(session: AsyncSession, listing_id: uuid.UUID) -> int:
    versions = tuple(
        (
            await session.scalars(
                select(ListingVersionRecord)
                .where(ListingVersionRecord.listing_id == listing_id)
                .order_by(ListingVersionRecord.observed_at.desc())
                .limit(BOUNDS.listing_versions_max + 1)
            )
        ).all()
    )
    excess = max(len(versions) - BOUNDS.listing_versions_max, 0)
    if excess == 0:
        return 0
    active_jobs = tuple(
        (
            await session.scalars(
                select(JobRecord)
                .where(
                    JobRecord.resource_key == _listing_key(listing_id),
                    JobRecord.status.in_(ACTIVE_JOB_STATUSES),
                )
                .limit(BOUNDS.job_queue_max)
            )
        ).all()
    )
    protected_fingerprints = {
        str(job.payload["fingerprint"])
        for job in active_jobs
        if isinstance(job.payload.get("fingerprint"), str)
    }
    stale_ids = tuple(
        version.id
        for version in reversed(versions)
        if version.fingerprint not in protected_fingerprints
    )[:excess]
    if len(stale_ids) != excess:
        raise RepositoryError("active jobs protect more listing versions than can be retained")
    if stale_ids:
        await session.execute(
            delete(ListingVersionRecord).where(ListingVersionRecord.id.in_(stale_ids))
        )
        logger.info(
            "listing_versions_pruned",
            extra={"listing_id": str(listing_id), "count": len(stale_ids)},
        )
    return len(stale_ids)


async def _listing_payload_for_fingerprint(
    session: AsyncSession,
    listing: ListingRecord,
    fingerprint: str,
) -> dict[str, Any]:
    if listing.fingerprint == fingerprint:
        return listing.payload
    version = await session.scalar(
        select(ListingVersionRecord).where(
            ListingVersionRecord.listing_id == listing.id,
            ListingVersionRecord.fingerprint == fingerprint,
        )
    )
    if version is None:
        raise ConflictError("job targets a listing version that is no longer retained")
    return version.payload


def _select_bootstrap_records(records: list[ListingRecord]) -> list[ListingRecord]:
    def key(record: ListingRecord) -> tuple[float, datetime]:
        listing = NormalizedListing.model_validate(record.payload)
        published = listing.published_at.timestamp() if listing.published_at is not None else 0.0
        return published, listing.observed_at

    return sorted(records, key=key, reverse=True)[: BOUNDS.bootstrap_assessments_max]


async def _enqueue_assessment(
    session: AsyncSession,
    record: ListingRecord,
    *,
    notify: bool,
    bootstrap_scan_id: uuid.UUID | None,
    capacity_checked: bool = True,
) -> JobRecord:
    payload = AssessmentJobPayload(
        listing_id=record.id,
        fingerprint=record.fingerprint,
        notify=notify,
        bootstrap_scan_id=bootstrap_scan_id,
    )
    group_key = _bootstrap_key(bootstrap_scan_id) if bootstrap_scan_id is not None else None
    return await enqueue_job(
        session,
        kind="assessment",
        resource_key=_listing_key(record.id),
        group_key=group_key,
        idempotency_key=f"assessment:{record.id}:{record.fingerprint}",
        payload=payload.model_dump(mode="json"),
        capacity_checked=capacity_checked,
    )


async def _enqueue_notification(
    session: AsyncSession,
    assessment_payload: AssessmentJobPayload,
    *,
    capacity_checked: bool,
) -> JobRecord:
    payload = NotificationJobPayload(
        listing_id=assessment_payload.listing_id,
        fingerprint=assessment_payload.fingerprint,
    )
    return await enqueue_job(
        session,
        kind="notification",
        resource_key=_listing_key(payload.listing_id),
        idempotency_key=f"notification:{payload.listing_id}:{payload.fingerprint}",
        payload=payload.model_dump(mode="json"),
        capacity_checked=capacity_checked,
    )


async def _enqueue_bootstrap_digest(
    session: AsyncSession,
    *,
    scan_id: uuid.UUID,
    selected: list[ListingRecord],
    eligible_total: int,
    capacity_checked: bool,
) -> JobRecord:
    payload = BootstrapDigestJobPayload(
        scan_id=scan_id,
        listing_ids=tuple(record.id for record in selected),
        eligible_total=eligible_total,
    )
    return await enqueue_job(
        session,
        kind="bootstrap_digest",
        resource_key=_bootstrap_key(scan_id),
        group_key=_bootstrap_key(scan_id),
        idempotency_key=f"bootstrap-digest:{scan_id}",
        payload=payload.model_dump(mode="json"),
        capacity_checked=capacity_checked,
    )


async def _append_event(
    session: AsyncSession,
    *,
    kind: str,
    entity_id: str,
    dedupe_key: str,
    payload: dict[str, Any],
) -> EventRecord:
    existing = await session.scalar(select(EventRecord).where(EventRecord.dedupe_key == dedupe_key))
    if existing is not None:
        return existing
    event = EventRecord(
        kind=kind,
        entity_id=entity_id,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def _locked_job(session: AsyncSession, job_id: uuid.UUID) -> JobRecord:
    job = await session.get(JobRecord, job_id, with_for_update=True)
    if job is None:
        raise NotFoundError("job not found")
    if job.status != "running":
        raise ConflictError(f"job {job_id} is not running")
    return job


async def _listing_view(session: AsyncSession, record: ListingRecord) -> ListingView:
    assessment_record = await session.scalar(
        select(AssessmentRecord).where(
            AssessmentRecord.listing_id == record.id,
            AssessmentRecord.fingerprint == record.fingerprint,
        )
    )
    feedback_record = await session.scalar(
        select(FeedbackRecord).where(FeedbackRecord.listing_id == record.id)
    )
    assessment = (
        ListingAssessment.model_validate(assessment_record.payload)
        if assessment_record is not None
        else None
    )
    feedback = cast(FeedbackValue, feedback_record.value) if feedback_record is not None else None
    return ListingView(
        id=record.id,
        active=record.active,
        eligibility=Eligibility.model_validate(record.eligibility),
        listing=NormalizedListing.model_validate(record.payload),
        assessment=assessment,
        feedback=feedback,
    )


def _watch_key(watch_id: uuid.UUID) -> str:
    return f"watch:{watch_id}"


def _compact_browser_scan_payload(job: JobRecord) -> None:
    if job.kind != "scan" or job.payload.get("trigger") != "browser":
        return
    capture = job.payload.get("browser_capture")
    if not isinstance(capture, dict):
        return
    job.payload = {
        "watch_id": job.payload.get("watch_id"),
        "watch_configuration_version": job.payload.get("watch_configuration_version"),
        "trigger": "browser",
        **browser_capture_metadata(capture),
    }


def _update_watch_after_browser_scan(watch: WatchRecord, source_url: str | None) -> None:
    if source_url is None:
        return
    now = _utc_now()
    watch.next_scan_at = now + timedelta(seconds=watch.poll_interval_s)
    watch.updated_at = now


def _listing_key(listing_id: uuid.UUID) -> str:
    return f"listing:{listing_id}"


def _bootstrap_key(scan_id: uuid.UUID) -> str:
    return f"bootstrap:{scan_id}"
