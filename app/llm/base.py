"""Базовый контракт LLM-провайдера и общий HTTP-транспорт.

Единый интерфейс из трёх сущностей: запрос (``LLMRequest``), ответ
(``LLMResult``) и клиент (``LLMClient``). Пайплайн суммаризации ничего не
знает о конкретном вендоре: подмена OpenAI на локальную Ollama - это одна
переменная окружения, а не правка кода.

Официальные SDK сознательно не используются: каждому провайдеру нужен ровно
один POST-запрос, а httpx уже есть в зависимостях. Взамен мы получаем
одинаковую логику таймаутов, ретраев и разбора ошибок для всех бэкендов.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Коды, при которых повтор запроса осмыслен: троттлинг и сбои на стороне
# провайдера. 4xx (кроме 429) повторять бессмысленно — запрос не изменится.
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class LLMError(Exception):
    """Ошибка обращения к модели.

    ``retryable`` отличает временный сбой (сеть, троттлинг) от постоянного
    (неверный ключ, несуществующая модель) - от этого зависит, стоит ли
    повторять запрос и какой HTTP-код вернуть клиенту.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class LLMRequest:
    system: str
    user: str
    # Текст части документа в чистом виде. Нужен только stub-провайдеру,
    # который вместо генерации применяет к нему регулярные эвристики.
    document_text: str = ""


@dataclass(slots=True)
class LLMResult:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    """Провайдер, умеющий вернуть JSON-ответ по системному и пользовательскому промпту."""

    name: str = "abstract"

    def __init__(self, *, model: str, timeout: float, temperature: float, max_output_tokens: int) -> None:
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResult:
        """Выполнить один вызов модели и вернуть сырой текст ответа."""

    async def _post(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST с JSON-телом и приведением ошибок к ``LLMError``."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Таймаут запроса к {self.name} ({self.timeout:.0f} c)", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Сетевая ошибка при обращении к {self.name}: {exc}", retryable=True
            ) from exc

        if response.status_code >= 400:
            snippet = response.text[:500]
            raise LLMError(
                f"{self.name} вернул HTTP {response.status_code}: {snippet}",
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(
                f"{self.name} вернул не-JSON ответ: {response.text[:300]}"
            ) from exc
