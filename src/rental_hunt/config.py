"""Validated process configuration with no implicit provider fallback."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    admin_api_token: SecretStr = Field(min_length=24)
    database_url: str = "postgresql+psycopg://rental_hunt:rental_hunt@localhost/rental_hunt"
    debug_artifact_dir: Path = Path("data/debug")
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = Field(
        default="rental-hunt-agent",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._ -]+$",
    )
    langsmith_tracing: bool = False
    langsmith_workspace_id: uuid.UUID | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    model_base_url: str | None = None
    model_name: str = Field(min_length=1)
    model_provider: Literal["ollama", "openai"]
    openai_api_key: SecretStr | None = None
    playwright_headless: bool = True
    playwright_user_data_dir: Path = Path("data/browser")
    source_mode: Literal["chrome_extension", "playwright"] = "chrome_extension"

    @model_validator(mode="after")
    def validate_provider(self) -> Settings:
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use postgresql+psycopg://")
        if self.model_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        if self.model_provider == "ollama" and self.model_base_url is None:
            raise ValueError("MODEL_BASE_URL is required when MODEL_PROVIDER=ollama")
        if self.langsmith_tracing and self.langsmith_api_key is None:
            raise ValueError("LANGSMITH_API_KEY is required when LANGSMITH_TRACING=true")
        return self

    @property
    def postgres_store_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
