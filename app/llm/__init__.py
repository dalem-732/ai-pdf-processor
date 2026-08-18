"""Абстракция над бэкендами генерации (OpenAI, Anthropic, Ollama, stub)."""

from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult
from app.llm.factory import build_client

__all__ = ["LLMClient", "LLMError", "LLMRequest", "LLMResult", "build_client"]
