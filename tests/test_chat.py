from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import ValidationError

from rental_hunt.chat import ChatError, ChatService, ChatSnapshot
from rental_hunt.config import Settings
from rental_hunt.contracts import ChatRequest, ListingPage
from rental_hunt.tracing import TraceManager


def _settings() -> Settings:
    return Settings(
        admin_api_token="a" * 24,
        database_url="postgresql+psycopg://test:test@localhost/test",
        langsmith_tracing=False,
        model_provider="openai",
        model_name="gpt-5.6-luna",
        openai_api_key="sk-test",
    )


class FakeModel:
    def __init__(
        self,
        content: str = "The queue is empty.",
        *,
        stream_chunks: tuple[str, ...] = ("The queue ", "is empty."),
    ) -> None:
        self.content = content
        self.stream_chunks = stream_chunks
        self.calls: list[tuple[Sequence[BaseMessage], dict[str, Any]]] = []

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        config: dict[str, Any],
    ) -> AIMessage:
        self.calls.append((messages, config))
        return AIMessage(content=self.content)

    async def astream(
        self,
        messages: Sequence[BaseMessage],
        *,
        config: dict[str, Any],
    ) -> AsyncIterator[AIMessage]:
        self.calls.append((messages, config))
        for chunk in self.stream_chunks:
            yield AIMessage(content=chunk)


def _snapshot() -> ChatSnapshot:
    return ChatSnapshot(
        preferences=None,
        watch=None,
        listings=ListingPage(items=(), next_cursor=None),
        active_jobs=0,
    )


@pytest.mark.asyncio
async def test_chat_is_read_only_bounded_and_reports_model() -> None:
    settings = _settings()
    model = FakeModel()
    service = ChatService(
        model=model,  # type: ignore[arg-type]
        model_provider="openai",
        model_name=settings.model_name,
        tracing=TraceManager(settings),
    )
    request = ChatRequest(messages=({"role": "user", "content": "Queue status?"},))

    response = await service.answer(request, _snapshot())

    assert response.message == "The queue is empty."
    assert response.trace_id is None
    assert response.model_name == "gpt-5.6-luna"
    messages, config = model.calls[0]
    assert "You cannot change preferences" in str(messages[0].content)
    assert messages[-1].content == "Queue status?"
    assert config["run_name"] == "rental-hunt-chat-model"


@pytest.mark.asyncio
async def test_chat_rejects_empty_or_oversized_model_output() -> None:
    settings = _settings()
    service = ChatService(
        model=FakeModel("   "),  # type: ignore[arg-type]
        model_provider="openai",
        model_name=settings.model_name,
        tracing=TraceManager(settings),
    )
    request = ChatRequest(messages=({"role": "user", "content": "Status?"},))

    with pytest.raises(ChatError, match="empty chat response"):
        await service.answer(request, _snapshot())


@pytest.mark.asyncio
async def test_chat_stream_emits_deltas_then_one_completion() -> None:
    settings = _settings()
    model = FakeModel(stream_chunks=("First ", "second"))
    service = ChatService(
        model=model,  # type: ignore[arg-type]
        model_provider="openai",
        model_name=settings.model_name,
        tracing=TraceManager(settings),
    )
    request = ChatRequest(messages=({"role": "user", "content": "Stream it"},))

    events = [event async for event in service.stream(request, _snapshot())]

    assert [event.type for event in events] == ["delta", "delta", "done"]
    assert [event.delta for event in events[:-1]] == ["First ", "second"]
    assert events[-1].message == "First second"
    assert events[-1].trace_id is None
    _, config = model.calls[0]
    assert config["run_name"] == "rental-hunt-chat-model-stream"


def test_chat_contract_requires_a_final_user_message() -> None:
    with pytest.raises(ValidationError, match="final chat message"):
        ChatRequest(messages=({"role": "assistant", "content": "Done"},))
