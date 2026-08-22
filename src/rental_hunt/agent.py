"""Narrow Deep Agents assessment surface with explicit provider and call budgets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langgraph.store.base import BaseStore

from rental_hunt.bounds import BOUNDS
from rental_hunt.config import Settings
from rental_hunt.contracts import (
    ListingAssessment,
    NormalizedListing,
    PreferencesUpdate,
    listing_fingerprint,
)
from rental_hunt.tracing import TraceManager

SKILL_NAMESPACE = ("rental-hunt", "skills")
MEMORY_NAMESPACE = ("rental-hunt", "memories")
SKILL_STORE_KEY = "/listing-analysis/SKILL.md"
MEMORY_STORE_KEY = "/preferences.md"

SYSTEM_PROMPT = """You assess one rental listing at a time.

The deterministic policy has already decided eligibility. Use the listing-analysis skill and the
read-only preference memory. Scores and prose annotate the alert; they never suppress it. Use only
facts in the supplied listing. Return the requested structured response."""


class AgentAssessmentError(RuntimeError):
    pass


def build_chat_model(settings: Settings) -> BaseChatModel:
    if settings.model_provider == "ollama":
        if settings.model_base_url is None:
            raise AssertionError("validated Ollama settings must contain MODEL_BASE_URL")
        return ChatOllama(
            model=settings.model_name,
            base_url=settings.model_base_url,
            temperature=0,
            # Thinking models can spend the entire output budget before emitting
            # the read/structured-output tool calls required by this service.
            reasoning=False,
            num_predict=BOUNDS.agent_output_tokens_max,
            validate_model_on_init=True,
            client_kwargs={"timeout": float(BOUNDS.agent_timeout_s)},
        )
    if settings.model_provider == "openai":
        if settings.openai_api_key is None:
            raise AssertionError("validated OpenAI settings must contain OPENAI_API_KEY")
        openai_options: dict[str, Any] = {
            "model": settings.model_name,
            "api_key": settings.openai_api_key.get_secret_value(),
            "temperature": 0,
            "max_completion_tokens": BOUNDS.agent_output_tokens_max,
            "timeout": 60.0,
            "max_retries": 1,
        }
        # GPT-5.6 Chat Completions function tools require effective reasoning `none`.
        # Deep Agents exposes read_file as a function tool, so relying on the model's
        # default `medium` effort would make the configured contract invalid.
        if settings.model_name.startswith("gpt-5.6"):
            openai_options["reasoning_effort"] = "none"
        return ChatOpenAI(
            base_url=settings.model_base_url,
            **openai_options,
        )
    raise AssertionError(f"unhandled model provider: {settings.model_provider}")


def _register_read_only_profile() -> None:
    profile = HarnessProfile(
        excluded_tools=frozenset({"delete", "edit_file", "execute", "task", "write_file"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile("ollama", profile)
    register_harness_profile("openai", profile)


class AgentService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: BaseStore,
        model: BaseChatModel | None = None,
        skill_path: Path | None = None,
        tracing: TraceManager | None = None,
    ) -> None:
        _register_read_only_profile()
        self.settings = settings
        self.store = store
        self.tracing = tracing or TraceManager(settings)
        self.skill_path = skill_path or (
            Path(__file__).parent / "skills" / "listing-analysis" / "SKILL.md"
        )
        selected_model = model if model is not None else build_chat_model(settings)
        backend = CompositeBackend(
            default=StateBackend(),
            routes={
                "/skills/": StoreBackend(namespace=lambda _runtime: SKILL_NAMESPACE),
                "/memories/": StoreBackend(namespace=lambda _runtime: MEMORY_NAMESPACE),
            },
        )
        response_format = (
            ProviderStrategy(ListingAssessment, strict=True)
            if settings.model_provider == "openai"
            else ToolStrategy(ListingAssessment)
        )
        agent_factory = cast(Callable[..., Any], create_deep_agent)
        self.graph = agent_factory(
            model=selected_model,
            system_prompt=SYSTEM_PROMPT,
            middleware=(
                ModelCallLimitMiddleware(
                    run_limit=BOUNDS.agent_model_calls_max,
                    exit_behavior="error",
                ),
                ToolCallLimitMiddleware(
                    run_limit=BOUNDS.agent_file_calls_max,
                    exit_behavior="error",
                ),
            ),
            subagents=(),
            skills=["/skills/"],
            memory=["/memories/preferences.md"],
            permissions=[
                FilesystemPermission(
                    operations=["write"],
                    paths=["/**"],
                    mode="deny",
                )
            ],
            backend=backend,
            store=store,
            response_format=response_format,
            name="rental-listing-assessor",
            debug=False,
        )

    async def seed_skill(self) -> None:
        content = self.skill_path.read_text(encoding="utf-8")
        await self.store.aput(
            SKILL_NAMESPACE,
            SKILL_STORE_KEY,
            cast(dict[str, Any], create_file_data(content)),
        )

    async def assess(
        self,
        listing: NormalizedListing,
        preferences: PreferencesUpdate,
        *,
        require_read_tool: bool = False,
    ) -> ListingAssessment:
        await self._put_preferences_memory(preferences)
        listing_json = json.dumps(
            listing.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        doctor_instruction = (
            "Call read_file exactly once for /skills/listing-analysis/SKILL.md. "
            "The preference memory is already supplied; do not read another file. "
            "Then return the structured assessment without another tool call.\n"
            if require_read_tool
            else ""
        )
        prompt = (
            doctor_instruction
            + "Assess this normalized listing. Deterministic eligibility has passed.\n\n"
            + f"```json\n{listing_json}\n```"
        )
        thread_id = f"assessment:{listing.source_listing_id}:{listing.fingerprint}"
        trace_tags: tuple[str, ...] = (
            "listing-assessment",
            self.settings.model_provider,
            self.settings.model_name,
        )
        if require_read_tool:
            trace_tags += ("doctor",)

        async def invoke_agent() -> ListingAssessment:
            try:
                async with asyncio.timeout(BOUNDS.agent_timeout_s):
                    result = await self.graph.ainvoke(
                        {"messages": [{"role": "user", "content": prompt}]},
                        config={
                            "configurable": {"thread_id": thread_id},
                            "metadata": {
                                "fingerprint": listing.fingerprint,
                                "source_listing_id": listing.source_listing_id,
                            },
                            "recursion_limit": BOUNDS.langgraph_recursion_limit,
                            "run_name": "rental-listing-deep-agent",
                            "tags": list(trace_tags),
                        },
                    )
            except TimeoutError as error:
                raise AgentAssessmentError(
                    f"agent exceeded the {BOUNDS.agent_timeout_s}s assessment timeout"
                ) from error
            return _parse_assessment_result(result, require_read_tool=require_read_tool)

        assessment, _trace_id = await self.tracing.arun(
            "rental-listing-assessment",
            inputs={
                "listing": listing.model_dump(mode="json"),
                "preferences": preferences.model_dump(mode="json"),
                "doctor": require_read_tool,
            },
            operation=invoke_agent,
            tags=trace_tags,
            metadata={
                "fingerprint": listing.fingerprint,
                "source_listing_id": listing.source_listing_id,
            },
        )
        return assessment

    async def doctor(self) -> ListingAssessment:
        observed_at = datetime.now(UTC)
        values: dict[str, Any] = {
            "source": "seloger",
            "source_listing_id": "doctor-1",
            "canonical_url": "https://www.seloger.com/annonces/doctor-1",
            "title": "Appartement test 2 pièces",
            "description": "Appartement calme et lumineux proche du métro.",
            "rent_eur_monthly": 1_500,
            "surface_m2": "45.00",
            "rooms": 2,
            "bedrooms": 1,
            "furnished": True,
            "postal_code": "75011",
            "city": "Paris",
            "photo_urls": [],
            "published_at": None,
            "observed_at": observed_at.isoformat(),
            "data_warnings": [],
        }
        values["fingerprint"] = listing_fingerprint(values)
        listing = NormalizedListing.model_validate(values)
        preferences = PreferencesUpdate(
            rent_eur_monthly_max=2_000,
            surface_m2_min="40.00",
            rooms_min=2,
            furnished="required",
            postal_codes_allowed=("75011",),
            soft_preferences=("quiet", "close to the metro"),
        )
        return await self.assess(listing, preferences, require_read_tool=True)

    async def _put_preferences_memory(self, preferences: PreferencesUpdate) -> None:
        lines = [
            "# Rental preferences",
            "",
            "## Hard constraints (already enforced by the service)",
            f"- Monthly rent at most EUR {preferences.rent_eur_monthly_max}",
            f"- Surface at least {preferences.surface_m2_min} m²",
            f"- Rooms minimum: {preferences.rooms_min or 'not configured'}",
            f"- Furnished: {preferences.furnished}",
            "- Allowed postal codes: "
            + (", ".join(preferences.postal_codes_allowed) or "not configured"),
            "",
            "## Soft preferences",
        ]
        if preferences.soft_preferences:
            lines.extend(f"- {value}" for value in preferences.soft_preferences)
        else:
            lines.append("- None configured")
        lines.extend(
            (
                "",
                "The assessment score is explanatory only and must never change eligibility.",
            )
        )
        await self.store.aput(
            MEMORY_NAMESPACE,
            MEMORY_STORE_KEY,
            cast(dict[str, Any], create_file_data("\n".join(lines))),
        )


def _parse_assessment_result(
    result: Mapping[str, Any],
    *,
    require_read_tool: bool,
) -> ListingAssessment:
    structured = result.get("structured_response")
    if require_read_tool and not _used_read_file(result):
        raise AgentAssessmentError("agent doctor observed no read_file tool call")
    if structured is None:
        raise AgentAssessmentError("agent returned no structured assessment")
    if isinstance(structured, ListingAssessment):
        return structured
    try:
        return ListingAssessment.model_validate(cast(Any, structured))
    except ValueError as error:
        raise AgentAssessmentError("agent returned an invalid structured assessment") from error


def _used_read_file(result: Mapping[str, Any]) -> bool:
    messages = result.get("messages", ())
    if not isinstance(messages, (list, tuple)):
        return False
    for message in messages[: BOUNDS.agent_recursion_limit * 2]:
        tool_calls = getattr(message, "tool_calls", ())
        if any(isinstance(call, dict) and call.get("name") == "read_file" for call in tool_calls):
            return True
    return False
