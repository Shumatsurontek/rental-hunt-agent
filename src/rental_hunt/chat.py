"""Bounded, read-only operator chat over a compact service-state snapshot."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from rental_hunt.bounds import BOUNDS
from rental_hunt.contracts import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ListingPage,
    PreferencesUpdate,
    WatchView,
)
from rental_hunt.tracing import TraceManager

SYSTEM_PROMPT = """You are the read-only operator assistant for Rental Hunt Agent.

Use only the supplied service snapshot and conversation. Clearly distinguish known facts from
missing data. You cannot change preferences, watches, jobs, feedback, or contact a landlord.
Direct the operator to the corresponding UI control or authenticated API when an action is needed.
Never claim an action succeeded. Keep answers concise and useful for debugging the deployment."""


class ChatError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatSnapshot:
    preferences: PreferencesUpdate | None
    watch: WatchView | None
    listings: ListingPage
    active_jobs: int

    def render(self) -> str:
        listing_values: list[dict[str, Any]] = []
        for item in self.listings.items[: BOUNDS.chat_context_listings_max]:
            listing = item.listing
            listing_values.append(
                {
                    "id": str(item.id),
                    "active": item.active,
                    "title": listing.title,
                    "url": str(listing.canonical_url),
                    "rent_eur_monthly": listing.rent_eur_monthly,
                    "surface_m2": str(listing.surface_m2) if listing.surface_m2 else None,
                    "rooms": listing.rooms,
                    "postal_code": listing.postal_code,
                    "city": listing.city,
                    "eligibility": item.eligibility.model_dump(mode="json"),
                    "assessment": (
                        item.assessment.model_dump(mode="json")
                        if item.assessment is not None
                        else None
                    ),
                    "feedback": item.feedback,
                }
            )
        payload = {
            "active_jobs": self.active_jobs,
            "preferences": (
                self.preferences.model_dump(mode="json") if self.preferences is not None else None
            ),
            "watch": self.watch.model_dump(mode="json") if self.watch is not None else None,
            "recent_listings": listing_values,
        }
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(rendered) > BOUNDS.chat_context_chars_max:
            raise ChatError("service snapshot exceeded the chat context bound")
        return rendered


class ChatService:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        model_provider: Literal["ollama", "openai"],
        model_name: str,
        tracing: TraceManager,
    ) -> None:
        self.model = model
        self.model_provider = model_provider
        self.model_name = model_name
        self.tracing = tracing

    async def answer(self, request: ChatRequest, snapshot: ChatSnapshot) -> ChatResponse:
        snapshot_text = snapshot.render()
        messages = _chat_messages(request, snapshot_text)

        async def invoke_model() -> str:
            try:
                async with asyncio.timeout(BOUNDS.chat_timeout_s):
                    response = await self.model.ainvoke(
                        messages,
                        config={
                            "run_name": "rental-hunt-chat-model",
                            "tags": ["operator-chat", self.model_provider],
                            "metadata": {"model_name": self.model_name},
                        },
                    )
            except TimeoutError as error:
                raise ChatError(f"chat exceeded the {BOUNDS.chat_timeout_s}s timeout") from error
            except Exception as error:
                raise ChatError(f"model chat failed with {type(error).__name__}") from error
            content = _message_text(response)
            if not content:
                raise ChatError("model returned an empty chat response")
            if len(content) > BOUNDS.chat_output_chars_max:
                raise ChatError("model returned a chat response above the output bound")
            return content

        content, trace_id = await self.tracing.arun(
            "rental-hunt-chat",
            inputs={
                "messages": [message.model_dump(mode="json") for message in request.messages],
                "snapshot": json.loads(snapshot_text),
            },
            operation=invoke_model,
            tags=("operator-chat", self.model_provider, self.model_name),
            metadata={"model_provider": self.model_provider, "model_name": self.model_name},
        )
        return ChatResponse(
            message=content,
            trace_id=trace_id,
            model_provider=self.model_provider,
            model_name=self.model_name,
        )

    async def stream(
        self,
        request: ChatRequest,
        snapshot: ChatSnapshot,
    ) -> AsyncIterator[ChatStreamEvent]:
        snapshot_text = snapshot.render()
        messages = _chat_messages(request, snapshot_text)

        async def invoke_model_stream() -> AsyncIterator[str]:
            chunks_seen = 0
            output_chars = 0
            try:
                async with asyncio.timeout(BOUNDS.chat_timeout_s):
                    async for response in self.model.astream(
                        messages,
                        config={
                            "run_name": "rental-hunt-chat-model-stream",
                            "tags": ["operator-chat", "stream", self.model_provider],
                            "metadata": {"model_name": self.model_name},
                        },
                    ):
                        chunks_seen += 1
                        if chunks_seen > BOUNDS.chat_stream_chunks_max:
                            raise ChatError("model exceeded the chat stream chunk bound")
                        delta = _message_delta(response)
                        if not delta:
                            continue
                        output_chars += len(delta)
                        if output_chars > BOUNDS.chat_output_chars_max:
                            raise ChatError("model returned a chat response above the output bound")
                        yield delta
            except TimeoutError as error:
                raise ChatError(f"chat exceeded the {BOUNDS.chat_timeout_s}s timeout") from error
            except ChatError:
                raise
            except Exception as error:
                raise ChatError(f"model chat failed with {type(error).__name__}") from error

        parts: list[str] = []
        trace_id = None
        async for delta, current_trace_id in self.tracing.astream(
            "rental-hunt-chat-stream",
            inputs={
                "messages": [message.model_dump(mode="json") for message in request.messages],
                "snapshot": json.loads(snapshot_text),
            },
            operation=invoke_model_stream,
            tags=("operator-chat", "stream", self.model_provider, self.model_name),
            metadata={"model_provider": self.model_provider, "model_name": self.model_name},
        ):
            parts.append(delta)
            trace_id = current_trace_id
            yield ChatStreamEvent(type="delta", delta=delta)
        content = "".join(parts).strip()
        if not content:
            raise ChatError("model returned an empty chat response")
        yield ChatStreamEvent(
            type="done",
            message=content,
            trace_id=trace_id,
            model_provider=self.model_provider,
            model_name=self.model_name,
        )


def _chat_messages(request: ChatRequest, snapshot_text: str) -> list[BaseMessage]:
    messages: list[BaseMessage] = [
        SystemMessage(content=SYSTEM_PROMPT + "\n\nService snapshot:\n" + snapshot_text)
    ]
    for message in request.messages:
        if message.role == "user":
            messages.append(HumanMessage(content=message.content))
        else:
            messages.append(AIMessage(content=message.content))
    return messages


def _message_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    parts: list[str] = []
    for part in message.content[:20]:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            value = part.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts).strip()


def _message_delta(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for part in message.content[:20]:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            value = part.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)
