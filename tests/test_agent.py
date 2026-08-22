from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from deepagents import HarnessProfile
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError

import rental_hunt.agent as agent_module
from rental_hunt.agent import (
    MEMORY_NAMESPACE,
    MEMORY_STORE_KEY,
    SKILL_NAMESPACE,
    SKILL_STORE_KEY,
    AgentAssessmentError,
    AgentService,
    build_chat_model,
)
from rental_hunt.bounds import BOUNDS
from rental_hunt.config import Settings
from rental_hunt.contracts import ListingAssessment, NormalizedListing, PreferencesUpdate


def _settings(provider: str = "openai", **overrides: object) -> Settings:
    values: dict[str, object] = {
        "admin_api_token": "a" * 24,
        "database_url": "postgresql+psycopg://test:test@localhost/test",
        "langsmith_tracing": False,
        "model_provider": provider,
        "model_name": "test-model",
        "model_base_url": "http://127.0.0.1:11434" if provider == "ollama" else None,
        "openai_api_key": "sk-test" if provider == "openai" else None,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.mark.parametrize(
    ("provider", "class_name", "expected"),
    [
        (
            "ollama",
            "ChatOllama",
            {
                "temperature": 0,
                "reasoning": False,
                "num_predict": BOUNDS.agent_output_tokens_max,
                "validate_model_on_init": True,
                "client_kwargs": {"timeout": float(BOUNDS.agent_timeout_s)},
            },
        ),
        (
            "openai",
            "ChatOpenAI",
            {
                "temperature": 0,
                "max_completion_tokens": BOUNDS.agent_output_tokens_max,
                "timeout": 60.0,
                "max_retries": 1,
            },
        ),
    ],
)
def test_explicit_provider_factories(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    class_name: str,
    expected: dict[str, object],
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_model(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(agent_module, class_name, fake_model)

    assert build_chat_model(_settings(provider)) is sentinel
    assert captured["model"] == "test-model"
    for key, value in expected.items():
        assert captured[key] == value


def test_gpt_5_6_openai_factory_disables_reasoning_for_chat_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_model(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agent_module, "ChatOpenAI", fake_model)

    build_chat_model(_settings(model_name="gpt-5.6-luna"))

    assert captured["reasoning_effort"] == "none"


def test_provider_configuration_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _settings("openai", openai_api_key=None)
    with pytest.raises(ValidationError):
        _settings("ollama", model_base_url=None)
    with pytest.raises(ValidationError):
        _settings("anthropic")


class FakeGraph:
    def __init__(self, *, include_read: bool = True) -> None:
        self.include_read = include_read
        self.invocations: list[tuple[dict[str, object], dict[str, object]]] = []

    async def ainvoke(
        self,
        value: dict[str, object],
        *,
        config: dict[str, object],
    ) -> dict[str, object]:
        self.invocations.append((value, config))
        tool_calls = (
            [
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/listing-analysis/SKILL.md"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ]
            if self.include_read
            else []
        )
        return {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "structured_response": {
                "score": 82,
                "confidence": "high",
                "summary": "Strong documented match.",
                "strengths": ["quiet"],
                "risks": [],
                "unknowns": ["commute time"],
            },
        }


def _service(
    monkeypatch: pytest.MonkeyPatch,
    graph: FakeGraph,
) -> tuple[AgentService, InMemoryStore, dict[str, Any], list[HarnessProfile]]:
    captured: dict[str, Any] = {}
    profiles: list[HarnessProfile] = []

    def fake_factory(**kwargs: Any) -> FakeGraph:
        captured.update(kwargs)
        return graph

    def fake_register(_key: str, profile: HarnessProfile) -> None:
        profiles.append(profile)

    monkeypatch.setattr(agent_module, "create_deep_agent", fake_factory)
    monkeypatch.setattr(agent_module, "register_harness_profile", fake_register)
    store = InMemoryStore()
    service = AgentService(
        settings=_settings(),
        store=store,
        model=object(),  # type: ignore[arg-type]
        skill_path=Path("src/rental_hunt/skills/listing-analysis/SKILL.md"),
    )
    return service, store, captured, profiles


@pytest.mark.asyncio
async def test_agent_surface_is_read_only_bounded_and_seeded(
    monkeypatch: pytest.MonkeyPatch,
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    service, store, captured, profiles = _service(monkeypatch, FakeGraph())
    await service.seed_skill()
    preferences = PreferencesUpdate(
        rent_eur_monthly_max=2_000,
        surface_m2_min=Decimal("40"),
        soft_preferences=("quiet",),
    )

    assessment = await service.assess(listing_factory(), preferences)

    assert assessment.score == 82
    assert captured["subagents"] == ()
    assert captured["skills"] == ["/skills/"]
    assert captured["memory"] == ["/memories/preferences.md"]
    assert isinstance(captured["response_format"], ProviderStrategy)
    permissions = captured["permissions"]
    assert len(permissions) == 1
    assert permissions[0].operations == ["write"]
    assert permissions[0].mode == "deny"
    middleware = captured["middleware"]
    model_limit = next(value for value in middleware if isinstance(value, ModelCallLimitMiddleware))
    tool_limit = next(value for value in middleware if isinstance(value, ToolCallLimitMiddleware))
    assert model_limit.run_limit == BOUNDS.agent_model_calls_max
    assert tool_limit.run_limit == BOUNDS.agent_file_calls_max
    assert all(
        {"task", "write_file", "edit_file", "delete", "execute"} <= profile.excluded_tools
        for profile in profiles
    )
    assert all(
        profile.general_purpose_subagent is not None
        and not profile.general_purpose_subagent.enabled
        for profile in profiles
    )
    skill = await store.aget(SKILL_NAMESPACE, SKILL_STORE_KEY)
    memory = await store.aget(MEMORY_NAMESPACE, MEMORY_STORE_KEY)
    assert skill is not None and "Listing analysis" in skill.value["content"]
    assert memory is not None and "quiet" in memory.value["content"]


@pytest.mark.asyncio
async def test_doctor_requires_real_read_tool_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = _service(monkeypatch, FakeGraph(include_read=False))

    with pytest.raises(AgentAssessmentError, match="no read_file tool call"):
        await service.doctor()


@pytest.mark.asyncio
async def test_doctor_requests_one_bounded_skill_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = FakeGraph()
    service, _, _, _ = _service(monkeypatch, graph)

    await service.doctor()

    request, config = graph.invocations[0]
    messages = request["messages"]
    assert isinstance(messages, list)
    prompt = messages[0]["content"]
    assert isinstance(prompt, str)
    assert "exactly once" in prompt
    assert "/skills/listing-analysis/SKILL.md" in prompt
    assert "/memories/preferences.md" not in prompt
    assert BOUNDS.agent_recursion_limit == 12
    assert config["recursion_limit"] == 13


def test_assessment_contract_rejects_out_of_budget_output() -> None:
    with pytest.raises(ValidationError):
        ListingAssessment(
            score=101,
            confidence="high",
            summary="invalid",
            strengths=(),
            risks=(),
            unknowns=(),
        )
