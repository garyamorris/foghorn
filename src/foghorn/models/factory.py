"""Provider factory: role (fast|smart|judge) x provider (offline|anthropic|openai|auto) -> chat model.

Model ids verified: installed anthropic SDK `Model` Literal + langchain_anthropic profiles +
platform.claude.com models overview (2026-11-08): claude-haiku-5-5 / claude-sonnet-5-5 / claude-opus-5-5 /
claude-fable-5-1. OpenAI ids verified in the openai SDK `ChatModel` Literal (tier semantics UNVERIFIED).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from foghorn.models.offline import SCRIPT_OVERRIDES, OfflineChatModel, Script
from foghorn.settings import Settings, get_settings

Role = Literal["fast", "smart", "judge"]

DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {"fast": "claude-haiku-5-5", "smart": "claude-sonnet-5-5", "judge": "claude-opus-5-5"},
    "openai": {"fast": "gpt-6-luna", "smart": "gpt-6-sol", "judge": "gpt-6.1-sol"},
}


def model_name(role: Role, provider: str, s: Settings) -> str:
    override = {"fast": s.model_fast, "smart": s.model_smart, "judge": s.model_judge}[role]
    return override or DEFAULT_MODELS[provider][role]


@lru_cache(maxsize=32)
def _anthropic(name: str, timeout: float, retries: int, max_tokens: int, api_key: str) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic  # lazy: offline mode must never need the key

    # NO temperature/top_p/top_k: rejected (sonnet-5-5, fable-5-x) or ignored/unsupported for Claude 5.
    return ChatAnthropic(model=name, api_key=api_key, timeout=timeout, max_retries=retries, max_tokens=max_tokens)


@lru_cache(maxsize=32)
def _openai(name: str, timeout: float, retries: int, max_tokens: int, api_key: str) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=name, api_key=api_key, timeout=timeout, max_retries=retries, max_completion_tokens=max_tokens
    )


def get_chat_model(
    role: Role, *, offline_script: Script | None = None, settings: Settings | None = None
) -> BaseChatModel:
    """Call INSIDE graph builders / nodes (cheap, cached for real providers), never at import time
    with side effects other than constructing objects. `offline_script` is only used when offline."""
    s = settings or get_settings()
    provider = s.resolved_provider()
    if provider == "offline":
        return OfflineChatModel(
            role=role,
            script=SCRIPT_OVERRIDES.get(role) or offline_script or Script(),
            token_delay_s=s.offline_token_delay_s,
        )
    key = s.anthropic_api_key if provider == "anthropic" else s.openai_api_key
    assert key is not None
    build = _anthropic if provider == "anthropic" else _openai
    return build(
        model_name(role, provider, s), s.llm_timeout_s, s.llm_max_retries, s.llm_max_tokens, key.get_secret_value()
    )


def structured(llm: BaseChatModel, schema: type[BaseModel] | dict[str, Any], **kw: Any) -> Any:
    """Single choke-point for with_structured_output so provider quirks live in one place.

    Claude 5.5 models reject forced tool_choice (langchain_anthropic._supports_forced_tool_choice) so the
    default `function_calling` method degrades to 'hope the model calls the tool'. Use json_schema.
    """
    name = type(llm).__name__
    if name == "ChatAnthropic":
        return llm.with_structured_output(schema, method="json_schema", **kw)
    return llm.with_structured_output(schema, **kw)
