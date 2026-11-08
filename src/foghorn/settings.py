"""Typed configuration. One place that reads the environment; everything else takes a Settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["auto", "offline", "anthropic", "openai"]


class Settings(BaseSettings):
    # `.env` is also loaded by `langgraph dev` (langgraph.json "env"); reading it here too keeps
    # `pytest`, scripts and the server consistent. Real env vars win over the file.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FOGHORN_",
        extra="ignore",
        env_file_encoding="utf-8",
        populate_by_name=True,  # Settings(anthropic_api_key=...) works despite validation_alias; extra="ignore" would silently drop it
    )

    # --- models -------------------------------------------------------------------------------
    provider: Provider = "offline"  # opt in to real models explicitly (FOGHORN_PROVIDER=auto|anthropic|openai)
    model_fast: str | None = None  # override the role->model table, e.g. FOGHORN_MODEL_FAST=claude-haiku-5-5
    model_smart: str | None = None
    model_judge: str | None = None
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    llm_max_tokens: int = 4096  # NEVER leave unset on Claude 5: langchain-anthropic defaults to 128000
    offline_token_delay_s: float = Field(0.0, ge=0, le=1)  # pace Studio demos, e.g. 0.03

    # --- provider credentials: standard names, SecretStr so repr()/logs never print them --------
    anthropic_api_key: SecretStr | None = Field(None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(None, validation_alias="OPENAI_API_KEY")

    # --- observability ---------------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False  # JSON lines in prod, console renderer in dev
    environment: Literal["dev", "test", "staging", "prod"] = "dev"

    # --- runtime locations / public URLs ----------------------------------------------------------
    data_dir: Path = Path("var")  # generated data + state DBs (git-ignored). Relative paths resolve against the CWD.
    public_base_url: str = "http://127.0.0.1:2026"  # where this server is reachable (Studio's baseUrl)
    studio_url: str = "https://smith.langchain.com/studio/"

    # --- web / security -------------------------------------------------------------------------
    webhook_secret: SecretStr = SecretStr("dev-only-change-me")
    webhook_max_body_bytes: int = 256_000
    webhook_rate_per_min: int = 120
    dashboard_api_token: SecretStr | None = None  # None => auth disabled (dev only; prod must set)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def resolved_provider(self) -> Literal["offline", "anthropic", "openai"]:
        if self.provider != "auto":
            return self.provider
        if self.anthropic_api_key and self.anthropic_api_key.get_secret_value():
            return "anthropic"
        if self.openai_api_key and self.openai_api_key.get_secret_value():
            return "openai"
        return "offline"


@lru_cache
def get_settings() -> Settings:
    return Settings()
