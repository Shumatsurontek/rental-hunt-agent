"""Authenticated FastAPI control plane."""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text

from rental_hunt.bounds import BOUNDS
from rental_hunt.chat import ChatError, ChatSnapshot
from rental_hunt.config import Settings
from rental_hunt.contracts import (
    AgentStatus,
    BrowserNotificationPage,
    BrowserScanCapture,
    ChatRequest,
    ChatResponse,
    FeedbackUpdate,
    FeedbackValue,
    FeedbackView,
    JobView,
    ListingPage,
    ManualScanAccepted,
    NotificationAck,
    PreferencesUpdate,
    WatchConfigurationUpdate,
    WatchConfigurationView,
    WatchCreate,
    WatchPatch,
    WatchView,
)
from rental_hunt.database import session_scope
from rental_hunt.repository import (
    ConflictError,
    MissingPreferencesError,
    NotFoundError,
    QueueFullError,
    acknowledge_notification,
    active_job_count,
    create_watch,
    enqueue_browser_scan,
    enqueue_manual_scan,
    get_current_watch,
    get_job,
    get_preferences,
    list_listings,
    list_pending_notifications,
    pending_notification_count,
    put_preferences,
    record_feedback,
    set_watch_enabled,
    update_watch_configuration,
)
from rental_hunt.runtime import ServiceRuntime
from rental_hunt.seloger import validate_seloger_url


def create_app(settings: Settings | None = None) -> FastAPI:  # noqa: PLR0915
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or Settings()
        runtime = await ServiceRuntime.create(resolved_settings)
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="Rental Hunt Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    _register_error_handlers(app)

    async def authorize(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = request.app.state.runtime.settings.admin_api_token.get_secret_value()
        supplied = ""
        if authorization is not None and authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    protected = [Depends(authorize)]

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> JSONResponse:
        runtime = _runtime(request)
        try:
            async with session_scope(runtime.session_factory) as session:
                await session.execute(text("SELECT 1"))
                queue_count = await active_job_count(session)
                notification_count = await pending_notification_count(session)
        except Exception as error:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": type(error).__name__},
            )
        if runtime.failed_task_names():
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": "background_task_stopped"},
            )
        if queue_count >= BOUNDS.job_queue_max:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": "job_queue_full"},
            )
        if notification_count >= BOUNDS.browser_notifications_pending_max:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": "notification_outbox_full"},
            )
        return JSONResponse(
            content={
                "status": "ready",
                "active_jobs": queue_count,
                "pending_notifications": notification_count,
            }
        )

    @app.put(
        "/v1/preferences",
        response_model=PreferencesUpdate,
        dependencies=protected,
    )
    async def update_preferences(
        request: Request,
        body: PreferencesUpdate,
    ) -> PreferencesUpdate:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            if await get_current_watch(session) is not None:
                raise ConflictError(
                    "edit active-watch preferences through its configuration endpoint"
                )
            await put_preferences(session, body)
        return body

    @app.get(
        "/v1/preferences",
        response_model=PreferencesUpdate,
        dependencies=protected,
    )
    async def current_preferences(request: Request) -> PreferencesUpdate:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            preferences = await get_preferences(session)
            if preferences is None:
                raise NotFoundError("preferences are not configured")
            return preferences

    @app.get(
        "/v1/agent/status",
        response_model=AgentStatus,
        dependencies=protected,
    )
    async def agent_status(request: Request) -> AgentStatus:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            queue_count = await active_job_count(session)
            notification_count = await pending_notification_count(session)
        return AgentStatus(
            model_provider=runtime.settings.model_provider,
            model_name=runtime.settings.model_name,
            source_mode=runtime.settings.source_mode,
            langsmith_tracing=runtime.tracing.enabled,
            langsmith_project=(runtime.tracing.project if runtime.tracing.enabled else None),
            active_jobs=queue_count,
            pending_notifications=notification_count,
            failed_background_tasks=runtime.failed_task_names(),
        )

    @app.post(
        "/v1/chat",
        response_model=ChatResponse,
        dependencies=protected,
    )
    async def chat(request: Request, body: ChatRequest) -> ChatResponse:
        runtime = _runtime(request)
        snapshot = await _load_chat_snapshot(runtime)
        return await runtime.chat.answer(body, snapshot)

    @app.post(
        "/v1/chat/stream",
        dependencies=protected,
        response_class=StreamingResponse,
    )
    async def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
        runtime = _runtime(request)
        snapshot = await _load_chat_snapshot(runtime)

        async def events() -> AsyncIterator[str]:
            try:
                async for event in runtime.chat.stream(body, snapshot):
                    yield _sse(event.type, event.model_dump(mode="json"))
            except ChatError as error:
                yield _sse("error", {"detail": str(error), "type": "error"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/v1/watches",
        response_model=WatchView,
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    async def post_watch(request: Request, body: WatchCreate) -> WatchView:
        try:
            validate_seloger_url(str(body.url))
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            if await get_preferences(session) is None:
                raise ConflictError("configure preferences before creating a watch")
            record = await create_watch(session, body)
            return WatchView.model_validate(record)

    @app.get(
        "/v1/watches/current",
        response_model=WatchView,
        dependencies=protected,
    )
    async def current_watch(request: Request) -> WatchView:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            record = await get_current_watch(session)
            if record is None:
                raise NotFoundError("no active watch")
            return WatchView.model_validate(record)

    @app.patch(
        "/v1/watches/{watch_id}",
        response_model=WatchView,
        dependencies=protected,
    )
    async def patch_watch(
        request: Request,
        watch_id: uuid.UUID,
        body: WatchPatch,
    ) -> WatchView:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            record = await set_watch_enabled(session, watch_id, enabled=body.enabled)
            return WatchView.model_validate(record)

    @app.put(
        "/v1/watches/{watch_id}/configuration",
        response_model=WatchConfigurationView,
        dependencies=protected,
    )
    async def put_watch_configuration(
        request: Request,
        watch_id: uuid.UUID,
        body: WatchConfigurationUpdate,
    ) -> WatchConfigurationView:
        try:
            validate_seloger_url(str(body.watch.url))
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            watch, preferences = await update_watch_configuration(session, watch_id, body)
            return WatchConfigurationView(
                watch=WatchView.model_validate(watch),
                preferences=preferences,
            )

    @app.post(
        "/v1/watches/{watch_id}/scan",
        response_model=ManualScanAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=protected,
    )
    async def manual_scan(request: Request, watch_id: uuid.UUID) -> ManualScanAccepted:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            job = await enqueue_manual_scan(session, watch_id)
            return ManualScanAccepted(job_id=job.id)

    @app.post(
        "/v1/watches/{watch_id}/browser-scan",
        response_model=ManualScanAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=protected,
    )
    async def browser_scan(
        request: Request,
        watch_id: uuid.UUID,
        body: BrowserScanCapture,
    ) -> ManualScanAccepted:
        for page in body.pages:
            try:
                validate_seloger_url(str(page.url))
                if page.next_url is not None:
                    validate_seloger_url(str(page.next_url))
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(error),
                ) from error
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            job = await enqueue_browser_scan(session, watch_id, body)
            return ManualScanAccepted(job_id=job.id)

    @app.get(
        "/v1/listings",
        response_model=ListingPage,
        dependencies=protected,
    )
    async def listings(
        request: Request,
        cursor: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        eligible_only: bool = False,
    ) -> ListingPage:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            return await list_listings(
                session,
                cursor=cursor,
                limit=limit,
                eligible_only=eligible_only,
            )

    @app.get(
        "/v1/notifications",
        response_model=BrowserNotificationPage,
        dependencies=protected,
    )
    async def notifications(
        request: Request,
        limit: Annotated[
            int,
            Query(ge=1, le=BOUNDS.browser_notifications_pull_max),
        ] = BOUNDS.browser_notifications_pull_max,
    ) -> BrowserNotificationPage:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            return await list_pending_notifications(session, limit=limit)

    @app.post(
        "/v1/notifications/{notification_id}/ack",
        response_model=NotificationAck,
        dependencies=protected,
    )
    async def notification_ack(
        request: Request,
        notification_id: uuid.UUID,
    ) -> NotificationAck:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            record = await acknowledge_notification(session, notification_id)
        return NotificationAck(id=record.id, status="sent")

    @app.put(
        "/v1/listings/{listing_id}/feedback",
        response_model=FeedbackView,
        dependencies=protected,
    )
    async def listing_feedback(
        request: Request,
        listing_id: uuid.UUID,
        body: FeedbackUpdate,
    ) -> FeedbackView:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            record = await record_feedback(
                session,
                event_id=body.event_id,
                listing_id=listing_id,
                value=body.value,
            )
        return FeedbackView(
            listing_id=record.listing_id,
            value=cast(FeedbackValue, record.value),
            updated_at=record.updated_at,
        )

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobView,
        dependencies=protected,
    )
    async def job(request: Request, job_id: uuid.UUID) -> JobView:
        runtime = _runtime(request)
        async with session_scope(runtime.session_factory) as session:
            return await get_job(session, job_id)

    return app


def _runtime(request: Request) -> ServiceRuntime:
    runtime: Any = request.app.state.runtime
    if not isinstance(runtime, ServiceRuntime):
        raise AssertionError("FastAPI runtime was not initialized")
    return runtime


async def _load_chat_snapshot(runtime: ServiceRuntime) -> ChatSnapshot:
    async with session_scope(runtime.session_factory) as session:
        preferences = await get_preferences(session)
        watch_record = await get_current_watch(session)
        watch = WatchView.model_validate(watch_record) if watch_record is not None else None
        listing_page = await list_listings(
            session,
            cursor=None,
            limit=BOUNDS.chat_context_listings_max,
        )
        queue_count = await active_job_count(session)
    return ChatSnapshot(
        preferences=preferences,
        watch=watch,
        listings=listing_page,
        active_jobs=queue_count,
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n"


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def not_found(_request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ConflictError)
    async def conflict(_request: Request, error: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(MissingPreferencesError)
    async def missing_preferences(
        _request: Request,
        error: MissingPreferencesError,
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(QueueFullError)
    async def queue_full(_request: Request, error: QueueFullError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.exception_handler(ChatError)
    async def chat_failed(_request: Request, error: ChatError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(error)})
