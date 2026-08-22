from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rental_hunt.api import create_app
from rental_hunt.config import Settings
from rental_hunt.contracts import BrowserNotificationPayload, ChatStreamEvent
from rental_hunt.database import session_scope
from rental_hunt.repository import create_browser_notification
from rental_hunt.runtime import ServiceRuntime


def _settings() -> Settings:
    return Settings(
        admin_api_token="a" * 24,
        database_url="postgresql+psycopg://test:test@localhost/test",
        langsmith_tracing=False,
        model_provider="openai",
        model_name="test-model",
        openai_api_key="sk-test",
    )


class FakeChat:
    async def stream(self, _body: object, _snapshot: object) -> AsyncIterator[ChatStreamEvent]:
        yield ChatStreamEvent(type="delta", delta="Hello ")
        yield ChatStreamEvent(
            type="done",
            message="Hello there",
            trace_id=uuid.UUID("00000000-0000-0000-0000-000000000123"),
            model_provider="openai",
            model_name="gpt-5.6-luna",
        )


@pytest.mark.asyncio
async def test_control_plane_auth_conflicts_and_pagination_bounds(  # noqa: PLR0915 - one API lifecycle.
    database: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = database
    settings = _settings()
    runtime = object.__new__(ServiceRuntime)
    runtime.settings = settings
    runtime.session_factory = factory
    runtime.tasks = ()
    runtime.tracing = type("Tracing", (), {"enabled": False, "project": None})()
    app = create_app(settings)
    app.state.runtime = runtime
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {settings.admin_api_token.get_secret_value()}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/v1/listings")).status_code == 401

        preference_response = await client.put(
            "/v1/preferences",
            headers=headers,
            json={
                "rent_eur_monthly_max": 2000,
                "surface_m2_min": "30.00",
                "rooms_min": 2,
                "furnished": "any",
                "postal_codes_allowed": ["75011"],
                "soft_preferences": ["quiet"],
            },
        )
        assert preference_response.status_code == 200
        assert (await client.get("/v1/preferences", headers=headers)).json() == (
            preference_response.json()
        )

        unsafe = await client.post(
            "/v1/watches",
            headers=headers,
            json={"url": "https://evil.example/list.htm"},
        )
        assert unsafe.status_code == 422
        created = await client.post(
            "/v1/watches",
            headers=headers,
            json={"url": "https://www.seloger.com/list.htm", "poll_interval_s": 600},
        )
        assert created.status_code == 201
        watch_id = cast(str, created.json()["id"])
        duplicate = await client.post(
            "/v1/watches",
            headers=headers,
            json={"url": "https://www.seloger.com/list.htm", "poll_interval_s": 600},
        )
        assert duplicate.status_code == 409
        current = await client.get("/v1/watches/current", headers=headers)
        assert current.status_code == 200
        assert current.json()["configuration_version"] == 1

        unsafe_configuration = await client.put(
            f"/v1/watches/{watch_id}/configuration",
            headers=headers,
            json={
                "watch": {"url": "https://evil.example/search", "poll_interval_s": 900},
                "preferences": preference_response.json(),
            },
        )
        assert unsafe_configuration.status_code == 422
        revised = await client.put(
            f"/v1/watches/{watch_id}/configuration",
            headers=headers,
            json={
                "watch": {
                    "url": "https://www.seloger.com/list.htm?projects=2",
                    "poll_interval_s": 900,
                },
                "preferences": {
                    **preference_response.json(),
                    "rent_eur_monthly_max": 2_200,
                },
            },
        )
        assert revised.status_code == 200
        assert revised.json()["watch"]["configuration_version"] == 2
        assert revised.json()["watch"]["baseline_complete"] is False
        assert revised.json()["watch"]["poll_interval_s"] == 900
        assert revised.json()["preferences"]["rent_eur_monthly_max"] == 2_200

        bypass_revision = await client.put(
            "/v1/preferences",
            headers=headers,
            json=preference_response.json(),
        )
        assert bypass_revision.status_code == 409

        unsafe_browser_capture = await client.post(
            f"/v1/watches/{watch_id}/browser-scan",
            headers=headers,
            json={
                "capture_id": "00000000-0000-0000-0000-000000000030",
                "watch_configuration_version": 2,
                "pages": [
                    {
                        "url": "https://www.seloger.com/list.htm",
                        "body_text": "1 annonce",
                        "document_html_prefix": "<html>",
                        "json_documents": [],
                        "dom_candidate_count": 0,
                        "dom_candidates": [],
                        "next_url": "https://evil.example/page-2",
                    }
                ],
            },
        )
        assert unsafe_browser_capture.status_code == 422

        accepted = await client.post(f"/v1/watches/{watch_id}/scan", headers=headers)
        assert accepted.status_code == 202
        job_id = cast(str, accepted.json()["job_id"])
        overlap = await client.post(f"/v1/watches/{watch_id}/scan", headers=headers)
        assert overlap.status_code == 409
        assert (await client.get(f"/v1/jobs/{job_id}", headers=headers)).status_code == 200
        assert (await client.get("/v1/listings?limit=101", headers=headers)).status_code == 422

        async with session_scope(factory) as session:
            notification, _ = await create_browser_notification(
                session,
                idempotency_key="bootstrap:api-test",
                kind="bootstrap_digest",
                listing_id=None,
                payload=BrowserNotificationPayload(
                    title="Baseline ready",
                    message="One eligible listing.",
                ),
            )
        assert (await client.get("/v1/notifications", headers=headers)).json()["items"][0][
            "id"
        ] == str(notification.id)
        assert (await client.get("/v1/notifications?limit=21", headers=headers)).status_code == 422
        acked = await client.post(
            f"/v1/notifications/{notification.id}/ack",
            headers=headers,
        )
        assert acked.status_code == 200
        assert acked.json() == {"id": str(notification.id), "status": "sent"}
        assert (await client.get("/v1/notifications", headers=headers)).json() == {"items": []}

        disabled = await client.patch(
            f"/v1/watches/{watch_id}",
            headers=headers,
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert (await client.get("/v1/watches/current", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_authenticated_chat_stream_is_sse_and_finishes_with_trace_id(
    database: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = database
    settings = _settings()
    runtime = object.__new__(ServiceRuntime)
    runtime.settings = settings
    runtime.session_factory = factory
    runtime.chat = FakeChat()  # type: ignore[assignment]
    runtime.tasks = ()
    app = create_app(settings)
    app.state.runtime = runtime
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {settings.admin_api_token.get_secret_value()}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/stream",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: delta\ndata: {"type":"delta","delta":"Hello "' in response.text
    assert "event: done" in response.text
    assert "00000000-0000-0000-0000-000000000123" in response.text
