"""Провайдер OpenAI Chat Completions.

Совместим с любым OpenAI-подобным API (Azure OpenAI, vLLM, LM Studio,
OpenRouter) - достаточно переопределить ``TENDER_OPENAI_BASE_URL``.
"""

from __future__ import annotations

from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(self, *, api_key: str, base_url: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(self, request: LLMRequest) -> LLMResult:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            # Гарантирует синтаксически корректный JSON на выходе; смысловую
            # валидацию всё равно делает Pydantic-схема выжимки.
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        data = await self._post(
            f"{self._base_url}/chat/completions",
            payload=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданная структура ответа OpenAI: {data}") from exc

        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            meta={"finish_reason": data["choices"][0].get("finish_reason")},
        )
