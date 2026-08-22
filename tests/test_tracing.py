from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

import rental_hunt.tracing as tracing_module
from rental_hunt.config import Settings
from rental_hunt.tracing import TraceManager


def _settings(*, enabled: bool) -> Settings:
    return Settings(
        admin_api_token="a" * 24,
        database_url="postgresql+psycopg://test:test@localhost/test",
        langsmith_api_key="lsv2_test" if enabled else None,
        langsmith_project="rental-hunt-tests",
        langsmith_tracing=enabled,
        model_provider="openai",
        model_name="test-model",
        openai_api_key="sk-test",
    )


@pytest.mark.asyncio
async def test_disabled_tracing_executes_without_allocating_trace_id() -> None:
    manager = TraceManager(_settings(enabled=False))

    result, trace_id = await manager.arun(
        "test-operation",
        inputs={"safe": True},
        operation=lambda: _return_value("done"),
    )

    assert result == "done"
    assert trace_id is None

    streamed = [
        item
        async for item in manager.astream(
            "test-stream",
            inputs={"safe": True},
            operation=lambda: _yield_values("one", "two"),
        )
    ]
    assert streamed == [("one", None), ("two", None)]


@pytest.mark.asyncio
async def test_traceable_call_gets_dynamic_client_project_and_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_options"] = kwargs

        def close(self, timeout: float | None = None) -> None:
            captured["close_timeout"] = timeout

    async def fake_traced_operation(
        operation: Callable[[], Awaitable[Any]],
        trace_inputs: Mapping[str, Any],
        *,
        langsmith_extra: Mapping[str, Any],
    ) -> Any:
        captured["trace_inputs"] = trace_inputs
        captured["langsmith_extra"] = langsmith_extra
        return await operation()

    monkeypatch.setattr(tracing_module, "Client", FakeClient)
    monkeypatch.setattr(tracing_module, "_traced_operation", fake_traced_operation)
    manager = TraceManager(_settings(enabled=True))
    requested_run_id = uuid.uuid4()

    result, trace_id = await manager.arun(
        "root-name",
        inputs={"safe": "payload"},
        operation=lambda: _return_value({"ok": True}),
        tags=("one",),
        metadata={"source": "test"},
        run_id=requested_run_id,
    )

    assert result == {"ok": True}
    assert trace_id == requested_run_id
    assert captured["trace_inputs"] == {"safe": "payload"}
    extra = captured["langsmith_extra"]
    assert extra["name"] == "root-name"
    assert extra["project_name"] == "rental-hunt-tests"
    assert extra["run_id"] == trace_id
    assert extra["tags"] == ["one"]
    assert extra["metadata"] == {"source": "test"}


def test_operation_entrypoint_is_decorated_with_traceable() -> None:
    assert hasattr(tracing_module._traced_operation, "__wrapped__")
    assert hasattr(tracing_module._traced_stream_operation, "__wrapped__")


async def _return_value(value: Any) -> Any:
    return value


async def _yield_values(*values: Any) -> Any:
    for value in values:
        yield value
