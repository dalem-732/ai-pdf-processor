"""Сборка LLM-клиента по настройкам."""

from __future__ import annotations

from app.config import LLMProvider, Settings
from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMClient, LLMError
from app.llm.ollama_client import OllamaClient
from app.llm.openai_client import OpenAIClient
from app.llm.stub_client import StubClient


def build_client(settings: Settings) -> LLMClient:
    """Вернуть клиента активного провайдера.

    Единственное место в коде, где выбирается вендор. Отсутствие ключа -
    ошибка конфигурации (``LLMError``), которую API превращает в 503, а не
    падение процесса.
    """
    common = {
        "timeout": settings.llm_timeout_seconds,
        "temperature": settings.llm_temperature,
        "max_output_tokens": settings.llm_max_output_tokens,
    }

    if settings.llm_provider is LLMProvider.OPENAI:
        if not settings.openai_api_key:
            raise LLMError("Не задан TENDER_OPENAI_API_KEY")
        return OpenAIClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            **common,
        )

    if settings.llm_provider is LLMProvider.ANTHROPIC:
        if not settings.anthropic_api_key:
            raise LLMError("Не задан TENDER_ANTHROPIC_API_KEY")
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            model=settings.anthropic_model,
            **common,
        )

    if settings.llm_provider is LLMProvider.OLLAMA:
        return OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            **common,
        )

    return StubClient(model="heuristic-stub", **common)
