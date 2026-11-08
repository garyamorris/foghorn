"""Provider quirks for structured output live in ONE helper (`foghorn.models.factory.structured`). The offline model
cannot validate schemas against a real provider, so these tests pin the choices that were learned the hard way."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from foghorn.models.factory import structured
from foghorn.models.offline import OfflineChatModel


class WithFreeFormDict(BaseModel):
    """A schema like the commander's PlanProposal: a free-form `params` mapping (not closed, so not strict-safe)."""

    action: str
    params: dict[str, Any] = Field(default_factory=dict)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def record(self: Any, schema: Any, **kw: Any) -> str:
        seen.append({"cls": type(self).__name__, **kw})
        return "runnable"

    for cls in (ChatOpenAI, ChatAnthropic):
        monkeypatch.setattr(cls, "with_structured_output", record)
    return seen


def test_openai_uses_function_calling_because_strict_json_schema_rejects_free_form_dicts(calls):
    structured(ChatOpenAI(model="gpt-6-luna", api_key="not-a-real-key"), WithFreeFormDict)  # type: ignore[arg-type]
    assert calls == [{"cls": "ChatOpenAI", "method": "function_calling"}]


def test_anthropic_uses_json_schema_because_claude_5_rejects_forced_tool_choice(calls):
    structured(ChatAnthropic(model="claude-sonnet-5-5", api_key="not-a-real-key"), WithFreeFormDict)  # type: ignore[arg-type]
    assert calls == [{"cls": "ChatAnthropic", "method": "json_schema"}]


def test_offline_model_is_unaffected_and_still_structures():
    out = structured(OfflineChatModel(role="fast"), WithFreeFormDict).invoke([("user", "hi")])
    assert isinstance(out, WithFreeFormDict)
