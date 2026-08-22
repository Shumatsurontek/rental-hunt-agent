"""Bounded LangSmith tracing with automatic LangChain child propagation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar, cast

from langsmith import Client, traceable, tracing_context
from pydantic import BaseModel

from rental_hunt.bounds import BOUNDS
from rental_hunt.config import Settings

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Keep callables and service objects out of the serialized trace input."""
    value = inputs.get("trace_inputs")
    if not isinstance(value, Mapping):
        raise AssertionError("trace_inputs must be a mapping")
    return dict(value)


def _trace_outputs(output: Any) -> dict[str, Any]:
    if isinstance(output, BaseModel):
        return {"result": output.model_dump(mode="json")}
    if isinstance(output, Mapping):
        return {"result": dict(output)}
    return {"result": output}


@traceable(
    name="rental-hunt-operation",
    run_type="chain",
    process_inputs=_trace_inputs,
    process_outputs=_trace_outputs,
)
async def _traced_operation(
    operation: Callable[[], Awaitable[Any]],
    trace_inputs: Mapping[str, Any],
) -> Any:
    """Run one root operation; nested LangChain calls inherit this trace context."""
    del trace_inputs  # Serialized by the decorator; the operation closure owns execution data.
    return await operation()


def _reduce_stream_chunks(chunks: Sequence[Any]) -> dict[str, Any]:
    if not all(isinstance(chunk, str) for chunk in chunks):
        raise AssertionError("traced stream chunks must be strings")
    return {"message": "".join(chunks)}


@traceable(
    name="rental-hunt-stream",
    run_type="chain",
    process_inputs=_trace_inputs,
    reduce_fn=_reduce_stream_chunks,
)
async def _traced_stream_operation(
    operation: Callable[[], AsyncIterator[Any]],
    trace_inputs: Mapping[str, Any],
) -> AsyncIterator[Any]:
    """Keep the trace context open until the caller consumes the bounded stream."""
    del trace_inputs
    async for item in operation():
        yield item


class TraceManager:
    def __init__(self, settings: Settings) -> None:
        self.project = settings.langsmith_project
        self._client: Client | None = None
        if settings.langsmith_tracing:
            if settings.langsmith_api_key is None:
                raise AssertionError("validated tracing settings must contain an API key")
            self._client = Client(
                api_key=settings.langsmith_api_key.get_secret_value(),
                timeout_ms=(3_000, 5_000),
                auto_batch_tracing=True,
                tracing_error_callback=_trace_error,
                workspace_id=(
                    str(settings.langsmith_workspace_id)
                    if settings.langsmith_workspace_id is not None
                    else None
                ),
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def arun(  # noqa: PLR0913 - trace envelopes keep correlation fields explicit.
        self,
        name: str,
        *,
        inputs: Mapping[str, Any],
        operation: Callable[[], Awaitable[T]],
        tags: tuple[str, ...] = (),
        metadata: Mapping[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
    ) -> tuple[T, uuid.UUID | None]:
        """Execute a bounded operation and return its stable LangSmith trace ID."""
        if self._client is None:
            return await operation(), None

        trace_id = run_id or uuid.uuid4()
        with tracing_context(
            enabled=True,
            project_name=self.project,
            client=self._client,
        ):
            result = await _traced_operation(
                operation,
                inputs,
                langsmith_extra={
                    "client": self._client,
                    "metadata": dict(metadata or {}),
                    "name": name,
                    "project_name": self.project,
                    "run_id": trace_id,
                    "tags": list(tags),
                },
            )
        return cast(T, result), trace_id

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            async with asyncio.timeout(BOUNDS.trace_flush_timeout_s + 1):
                await asyncio.to_thread(self._client.close, BOUNDS.trace_flush_timeout_s)
        except Exception as error:
            logger.warning("langsmith_trace_flush_failed", exc_info=error)

    async def astream(  # noqa: PLR0913 - streaming uses the same explicit envelope.
        self,
        name: str,
        *,
        inputs: Mapping[str, Any],
        operation: Callable[[], AsyncIterator[T]],
        tags: tuple[str, ...] = (),
        metadata: Mapping[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
    ) -> AsyncIterator[tuple[T, uuid.UUID | None]]:
        """Consume one bounded stream while preserving its root trace context."""
        if self._client is None:
            async for item in operation():
                yield item, None
            return

        trace_id = run_id or uuid.uuid4()
        with tracing_context(
            enabled=True,
            project_name=self.project,
            client=self._client,
        ):
            stream = _traced_stream_operation(
                operation,
                inputs,
                langsmith_extra={
                    "client": self._client,
                    "metadata": dict(metadata or {}),
                    "name": name,
                    "project_name": self.project,
                    "run_id": trace_id,
                    "tags": list(tags),
                },
            )
            async for item in stream:
                yield cast(T, item), trace_id


def _trace_error(error: Exception) -> None:
    logger.warning(
        "langsmith_trace_write_failed",
        extra={"error": str(error), "error_type": type(error).__name__},
    )
