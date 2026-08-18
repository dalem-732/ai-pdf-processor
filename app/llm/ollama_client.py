"""Провайдер Ollama - бесплатная локальная модель без ключей и внешней сети.

Вариант по умолчанию для тех, кто не хочет отправлять тендерную документацию
в облако. Проверялось на ``qwen2.5:7b-instruct`` (устойчиво держит русский
язык и формат JSON) и ``llama3.1:8b-instruct``.
"""

from __future__ import annotations

from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult


class OllamaClient(LLMClient):
    name = "ollama"

    def __init__(self, *, base_url: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._base_url = base_url.rstrip("/")

    async def complete(self, request: LLMRequest) -> LLMResult:
        payload = {
            "model": self.model,
            "stream": False,
            # format=json включает грамматически ограниченную генерацию -
            # локальная 7B-модель без этого регулярно скатывается в markdown.
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_output_tokens,
                # Документация тендера длинная: контекст по умолчанию (2048)
                # обрезал бы половину чанка.
                "num_ctx": 8192,
            },
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        data = await self._post(f"{self._base_url}/api/chat", payload=payload)

        try:
            text = data["message"]["content"] or ""
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Неожиданная структура ответа Ollama: {data}") from exc

        return LLMResult(
            text=text,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            meta={"done_reason": data.get("done_reason")},
        )
