"""Провайдер без модели: выжимка собирается регулярными правилами.

Нужен для трёх сценариев:

* демонстрация и приёмка API без ключей, без сети и без GPU;
* детерминированные тесты пайплайна (реальная LLM не даёт воспроизводимого
  ответа, и тесты на ней превращаются в проверку погоды);
* деградация вместо отказа, если облачный провайдер недоступен, а ответ
  нужен хоть какой-то.

Клиент реализует тот же интерфейс, что и остальные, поэтому пайплайн не
знает о подмене: на вход - промпт, на выходе - JSON-строка нужной формы.
"""

from __future__ import annotations

from app.core.heuristics import heuristic_summary
from app.llm.base import LLMClient, LLMRequest, LLMResult


class StubClient(LLMClient):
    name = "stub"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]

    async def complete(self, request: LLMRequest) -> LLMResult:
        summary = heuristic_summary(request.document_text)
        return LLMResult(
            text=summary.model_dump_json(exclude_none=False),
            prompt_tokens=None,
            completion_tokens=None,
            meta={"mode": "heuristic"},
        )
