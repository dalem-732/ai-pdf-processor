"""Провайдер Anthropic Messages API."""

from __future__ import annotations

import re

from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult

ANTHROPIC_API_VERSION = "2023-06-01"

# Claude Opus 4.7+, Sonnet 4.6+ и Sonnet 5 не принимают temperature и prefill
# последним assistant-сообщением - оба дают HTTP 400.
_MODERN_ANTHROPIC_MODEL_RE = re.compile(
    r"claude-(?:opus-4-(?:7|[89]|\d{2})|opus-5|sonnet-5|sonnet-4-(?:[6-9]|\d{2}))(?:$|-)",
    re.IGNORECASE,
)


def _anthropic_uses_modern_api(model: str) -> bool:
    return _MODERN_ANTHROPIC_MODEL_RE.search(model) is not None


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def _build_payload(self, request: LLMRequest) -> tuple[dict[str, object], bool]:
        uses_prefill = not _anthropic_uses_modern_api(self.model)
        messages: list[dict[str, str]] = [{"role": "user", "content": request.user}]
        if uses_prefill:
            # На старых моделях открывающая «{» в assistant-turn стабильнее
            # заставляет модель вернуть JSON, а не вступление.
            messages.append({"role": "assistant", "content": "{"})

        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": request.system,
            "messages": messages,
        }
        if not _anthropic_uses_modern_api(self.model):
            payload["temperature"] = self.temperature
        return payload, uses_prefill

    async def complete(self, request: LLMRequest) -> LLMResult:
        payload, uses_prefill = self._build_payload(request)
        data = await self._post(
            f"{self._base_url}/messages",
            payload=payload,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "Content-Type": "application/json",
            },
        )

        try:
            blocks = data["content"]
            text = "".join(
                block.get("text", "") for block in blocks if block.get("type") == "text"
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMError(f"Неожиданная структура ответа Anthropic: {data}") from exc

        if uses_prefill and not text.lstrip().startswith("{"):
            text = "{" + text

        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            meta={"stop_reason": data.get("stop_reason")},
        )
