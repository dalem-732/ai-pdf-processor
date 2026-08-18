"""Настройки сервиса.

Все параметры читаются из переменных окружения с префиксом ``TENDER_``
либо из файла ``.env`` в корне проекта. Значения по умолчанию подобраны так,
чтобы сервис поднимался и отвечал без единого секрета: провайдер ``stub``
собирает выжимку детерминированными эвристиками, без обращения к сети.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, Enum):
    """Поддерживаемые бэкенды генерации."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    STUB = "stub"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TENDER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: LLMProvider = LLMProvider.STUB

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-latest"
    anthropic_base_url: str = "https://api.anthropic.com/v1"

    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_base_url: str = "http://localhost:11434"

    max_upload_mb: int = Field(default=25, ge=1, le=200)
    chunk_chars: int = Field(default=12_000, ge=1_000, le=100_000)
    chunk_overlap_chars: int = Field(default=800, ge=0, le=10_000)
    max_chunks: int = Field(default=20, ge=1, le=200)

    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=4_000, ge=256, le=32_000)

    log_level: str = "INFO"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def active_model(self) -> str:
        """Имя модели активного провайдера - для эха в ответе API."""
        return {
            LLMProvider.OPENAI: self.openai_model,
            LLMProvider.ANTHROPIC: self.anthropic_model,
            LLMProvider.OLLAMA: self.ollama_model,
            LLMProvider.STUB: "heuristic-stub",
        }[self.llm_provider]

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        # Перекрытие больше самого чанка означало бы бесконечный цикл в
        # разбиении, поэтому обрезаем его до половины окна.
        if self.chunk_overlap_chars >= self.chunk_chars:
            object.__setattr__(self, "chunk_overlap_chars", self.chunk_chars // 2)
        return self

    def missing_credentials(self) -> str | None:
        """Причина, по которой провайдер не готов к работе, либо ``None``.

        Проверка вынесена отдельно, чтобы сервис поднимался даже с неполной
        конфигурацией и отвечал понятным 503 вместо падения на старте.
        """
        if self.llm_provider is LLMProvider.OPENAI and not self.openai_api_key:
            return "не задан TENDER_OPENAI_API_KEY для провайдера openai"
        if self.llm_provider is LLMProvider.ANTHROPIC and not self.anthropic_api_key:
            return "не задан TENDER_ANTHROPIC_API_KEY для провайдера anthropic"
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кешированные настройки (переиспользуются как FastAPI-зависимость)."""
    return Settings()
