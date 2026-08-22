"""Bounded scheduler and durable scan, assessment, and outbox job handlers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rental_hunt.agent import AgentService
from rental_hunt.bounds import BOUNDS
from rental_hunt.browser_notifications import (
    format_bootstrap_notification,
    format_listing_notification,
)
from rental_hunt.capture_metadata import browser_capture_metadata
from rental_hunt.contracts import (
    AssessmentJobPayload,
    BootstrapDigestJobPayload,
    NotificationJobPayload,
    ScanJobPayload,
    ScanResult,
)
from rental_hunt.database import session_scope
from rental_hunt.models import JobRecord
from rental_hunt.repository import (
    ConflictError,
    QueueFullError,
    bootstrap_assessments_pending,
    claim_jobs,
    cleanup_history,
    complete_job,
    create_browser_notification,
    defer_job,
    enqueue_due_scan,
    fail_job,
    fail_scan_run,
    get_assessment_context,
    get_digest_entries,
    get_notification_context,
    get_watch,
    persist_scan_result,
    save_assessment,
    start_scan_run,
)
from rental_hunt.seloger import SeLogerSource, SourceError, parse_browser_capture
from rental_hunt.tracing import TraceManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobClaim:
    id: uuid.UUID
    kind: str
    payload: dict[str, object]
    attempts: int
    trace_id: uuid.UUID | None

    @classmethod
    def from_record(cls, record: JobRecord) -> JobClaim:
        return cls(
            id=record.id,
            kind=record.kind,
            payload=dict(record.payload),
            attempts=record.attempts,
            trace_id=record.trace_id,
        )


class DeferJobError(RuntimeError):
    def __init__(self, delay_s: int) -> None:
        super().__init__(f"job deferred for {delay_s}s")
        self.delay_s = delay_s


class FatalJobError(RuntimeError):
    pass


class JobWorker:
    def __init__(  # noqa: PLR0913 - worker dependencies are explicit process resources.
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        source: SeLogerSource,
        agent: AgentService,
        model_provider: str,
        model_name: str,
        tracing: TraceManager,
    ) -> None:
        self.session_factory = session_factory
        self.source = source
        self.agent = agent
        self.model_provider = model_provider
        self.model_name = model_name
        self.tracing = tracing
        self._scan_slots = asyncio.Semaphore(BOUNDS.scan_concurrency_max)
        self._assessment_slots = asyncio.Semaphore(BOUNDS.assessment_concurrency_max)
        self._notification_slots = asyncio.Semaphore(BOUNDS.notification_prepare_concurrency_max)

    async def run_once(self) -> int:
        async with session_scope(self.session_factory) as session:
            records = await claim_jobs(session, trace_jobs=self.tracing.enabled)
            claims = tuple(JobClaim.from_record(record) for record in records)
        if not claims:
            return 0
        async with asyncio.TaskGroup() as task_group:
            for claim in claims:
                task_group.create_task(self._process_claim(claim), name=f"job-{claim.id}")
        return len(claims)

    async def _process_claim(self, claim: JobClaim) -> None:
        outcome, trace_id = await self.tracing.arun(
            "rental-hunt-job",
            inputs={
                "attempt": claim.attempts,
                "job_id": str(claim.id),
                "kind": claim.kind,
                "payload": _trace_job_payload(claim),
            },
            operation=lambda: self._process_claim_result(claim),
            tags=("durable-job", claim.kind),
            metadata={"attempt": claim.attempts, "job_id": str(claim.id)},
            run_id=claim.trace_id,
        )
        if trace_id is not None:
            logger.info(
                "job_trace_completed",
                extra={
                    "job_id": str(claim.id),
                    "status": outcome,
                    "trace_id": str(trace_id),
                },
            )

    async def _process_claim_result(self, claim: JobClaim) -> str:
        try:
            await self._dispatch(claim)
        except DeferJobError as deferred:
            async with session_scope(self.session_factory) as session:
                await defer_job(session, claim.id, delay_s=deferred.delay_s)
            return "deferred"
        except FatalJobError as error:
            await self._record_failure(claim, error, attempts_max=claim.attempts)
            return "dead"
        except Exception as error:
            attempts_max = _attempts_max(claim.kind)
            terminal = await self._record_failure(claim, error, attempts_max=attempts_max)
            return "dead" if terminal else "retrying"
        async with session_scope(self.session_factory) as session:
            await complete_job(session, claim.id)
        return "succeeded"

    async def _record_failure(
        self,
        claim: JobClaim,
        error: Exception,
        *,
        attempts_max: int,
    ) -> bool:
        logger.error(
            "job_failed",
            extra={
                "attempt": claim.attempts,
                "error": str(error),
                "error_type": type(error).__name__,
                "job_id": str(claim.id),
                "job_kind": claim.kind,
            },
            exc_info=error,
        )
        async with session_scope(self.session_factory) as session:
            return await fail_job(
                session,
                claim.id,
                error=f"{type(error).__name__}: {error}",
                attempts_max=attempts_max,
            )

    async def _dispatch(self, claim: JobClaim) -> None:
        if claim.kind == "scan":
            async with self._scan_slots:
                await self._handle_scan(claim)
            return
        if claim.kind == "assessment":
            async with self._assessment_slots:
                await self._handle_assessment(claim)
            return
        if claim.kind == "notification":
            async with self._notification_slots:
                await self._handle_notification(claim)
            return
        if claim.kind == "bootstrap_digest":
            async with self._notification_slots:
                await self._handle_bootstrap_digest(claim)
            return
        raise AssertionError(f"unhandled job kind: {claim.kind}")

    async def _handle_scan(self, claim: JobClaim) -> None:
        payload = ScanJobPayload.model_validate(claim.payload)
        async with session_scope(self.session_factory) as session:
            watch = await get_watch(session, payload.watch_id)
            if not watch.enabled:
                logger.info(
                    "disabled_watch_scan_skipped",
                    extra={"job_id": str(claim.id), "watch_id": str(watch.id)},
                )
                return
            if watch.configuration_version != payload.watch_configuration_version:
                raise FatalJobError("scan belongs to an outdated watch configuration")
            scan = await start_scan_run(session, job_id=claim.id, watch_id=watch.id)
            if scan.status == "succeeded":
                logger.info(
                    "scan_already_persisted",
                    extra={"job_id": str(claim.id), "scan_id": str(scan.id)},
                )
                return
            if scan.status != "running":
                raise FatalJobError(f"scan run cannot resume from status {scan.status!r}")
            scan_id = scan.id
            url = watch.url

        try:
            source_url: str | None = None
            if payload.trigger == "browser":
                if payload.browser_capture is None:
                    raise AssertionError("validated browser scan is missing its capture")
                result = parse_browser_capture(payload.browser_capture)
                source_url = str(payload.browser_capture.pages[0].url)
            else:
                async with asyncio.timeout(BOUNDS.scan_timeout_s):
                    result = await self._scan_with_retries(url)
        except (SourceError, TimeoutError) as error:
            code = error.code if isinstance(error, SourceError) else "scan_timeout"
            async with session_scope(self.session_factory) as session:
                await fail_scan_run(
                    session,
                    scan_id,
                    error_code=code,
                    error_detail=str(error),
                )
            raise

        try:
            async with session_scope(self.session_factory) as session:
                outcome = await persist_scan_result(
                    session,
                    scan_id=scan_id,
                    watch_id=payload.watch_id,
                    watch_configuration_version=payload.watch_configuration_version,
                    result=result,
                    source_url=source_url,
                )
        except Exception as error:
            async with session_scope(self.session_factory) as session:
                await fail_scan_run(
                    session,
                    scan_id,
                    error_code="persist_failed",
                    error_detail=f"{type(error).__name__}: {error}",
                )
            raise
        logger.info(
            "scan_succeeded",
            extra={
                "changed_count": outcome.changed_count,
                "discovered_count": outcome.discovered_count,
                "eligible_new_count": outcome.eligible_new_count,
                "scan_id": str(outcome.scan_id),
            },
        )

    async def _scan_with_retries(self, url: str) -> ScanResult:
        last_error: SourceError | TimeoutError | None = None
        for attempt in range(BOUNDS.source_attempts_max):
            try:
                return await self.source.scan(url)
            except (SourceError, TimeoutError) as error:
                last_error = error
                if attempt + 1 < BOUNDS.source_attempts_max:
                    await asyncio.sleep(2**attempt)
        if last_error is None:
            raise AssertionError("source retry loop ended without an error")
        raise last_error

    async def _handle_assessment(self, claim: JobClaim) -> None:
        payload = AssessmentJobPayload.model_validate(claim.payload)
        async with session_scope(self.session_factory) as session:
            context = await get_assessment_context(session, payload)
        assessment = await self.agent.assess(context.listing, context.preferences)
        async with session_scope(self.session_factory) as session:
            await save_assessment(
                session,
                payload=payload,
                assessment=assessment,
                model_provider=self.model_provider,
                model_name=self.model_name,
            )

    async def _handle_notification(self, claim: JobClaim) -> None:
        payload = NotificationJobPayload.model_validate(claim.payload)
        try:
            async with session_scope(self.session_factory) as session:
                entry = await get_notification_context(session, payload)
                await create_browser_notification(
                    session,
                    idempotency_key=f"listing:{payload.listing_id}:{payload.fingerprint}",
                    kind="listing",
                    listing_id=payload.listing_id,
                    payload=format_listing_notification(entry),
                )
        except QueueFullError as error:
            raise DeferJobError(60) from error

    async def _handle_bootstrap_digest(self, claim: JobClaim) -> None:
        payload = BootstrapDigestJobPayload.model_validate(claim.payload)
        try:
            async with session_scope(self.session_factory) as session:
                if await bootstrap_assessments_pending(session, payload.scan_id):
                    raise DeferJobError(5)
                entries = await get_digest_entries(session, payload)
                await create_browser_notification(
                    session,
                    idempotency_key=f"bootstrap:{payload.scan_id}",
                    kind="bootstrap_digest",
                    listing_id=None,
                    payload=format_bootstrap_notification(
                        entries,
                        eligible_total=payload.eligible_total,
                    ),
                )
        except QueueFullError as error:
            raise DeferJobError(60) from error


async def scheduler_loop(
    stop_event: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    while not stop_event.is_set():
        try:
            async with session_scope(session_factory) as session:
                await enqueue_due_scan(session)
        except ConflictError as error:
            logger.debug("scheduler_scan_already_active", extra={"error": str(error)})
        except Exception as error:
            logger.error("scheduler_tick_failed", exc_info=error)
        await _wait_or_stop(stop_event, BOUNDS.worker_tick_s)


async def worker_loop(stop_event: asyncio.Event, worker: JobWorker) -> None:
    while not stop_event.is_set():
        try:
            count = await worker.run_once()
        except Exception as error:
            logger.error("worker_tick_failed", exc_info=error)
            count = 0
        if count == 0:
            await _wait_or_stop(stop_event, BOUNDS.worker_tick_s)


async def cleanup_loop(
    stop_event: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    while not stop_event.is_set():
        try:
            async with session_scope(session_factory) as session:
                scan_count, job_count, notification_count = await cleanup_history(session)
            if scan_count or job_count or notification_count:
                logger.info(
                    "history_pruned",
                    extra={
                        "job_count": job_count,
                        "notification_count": notification_count,
                        "scan_count": scan_count,
                    },
                )
        except Exception as error:
            logger.error("cleanup_tick_failed", exc_info=error)
        await _wait_or_stop(stop_event, BOUNDS.cleanup_tick_s)


async def _wait_or_stop(stop_event: asyncio.Event, timeout_s: int) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_s)
    except TimeoutError:
        return


def _attempts_max(kind: str) -> int:
    if kind == "assessment":
        return BOUNDS.assessment_attempts_max
    if kind in {"notification", "bootstrap_digest"}:
        return BOUNDS.notification_prepare_attempts_max
    if kind == "scan":
        return 1
    raise AssertionError(f"unhandled job kind: {kind}")


def _trace_job_payload(claim: JobClaim) -> dict[str, object]:
    payload = dict(claim.payload)
    capture = payload.get("browser_capture")
    if not isinstance(capture, dict):
        return payload
    return {
        "watch_id": payload.get("watch_id"),
        "watch_configuration_version": payload.get("watch_configuration_version"),
        "trigger": payload.get("trigger"),
        **browser_capture_metadata(capture),
    }
