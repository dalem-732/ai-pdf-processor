"""Схемы данных API и внутреннего представления выжимки.

Модели служат одновременно тремя вещами:
1. контрактом HTTP-ответа (документируется в OpenAPI автоматически);
2. схемой, которую мы передаём LLM как описание требуемого JSON;
3. валидатором ответа модели - всё, что не проходит валидацию, отбрасывается
   или чинится, а не уезжает клиенту.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RequirementCategory(str, Enum):
    """Группировка требований к исполнителю по типовым разделам закупки."""

    LICENSE = "license"
    QUALIFICATION = "qualification"
    EXPERIENCE = "experience"
    STAFF = "staff"
    EQUIPMENT = "equipment"
    FINANCIAL = "financial"
    DOCUMENTS = "documents"
    OTHER = "other"


class Money(BaseModel):
    """Денежная величина с сохранением исходной формулировки."""

    model_config = ConfigDict(extra="ignore")

    amount: float | None = Field(
        default=None, description="Числовое значение, приведённое к float"
    )
    currency: str = Field(default="RUB", description="Код валюты, по умолчанию RUB")
    includes_vat: bool | None = Field(
        default=None, description="Указано ли в документе, что цена включает НДС"
    )
    raw: str | None = Field(
        default=None, description="Дословная цитата из документа, как есть"
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, value: Any) -> Any:
        """LLM часто отдаёт "1 234 567,89" или "1,234,567.89" строкой."""
        if value is None or isinstance(value, int | float):
            return value
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("\u00a0", "").replace(" ", "")
        if "," in text and "." in text:
            # Последний разделитель считаем десятичным.
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None


class Stage(BaseModel):
    """Этап исполнения контракта."""

    model_config = ConfigDict(extra="ignore")

    name: str
    deadline: str | None = Field(
        default=None, description="Срок этапа в формулировке документа"
    )


class Deadlines(BaseModel):
    """Сроки исполнения контракта."""

    model_config = ConfigDict(extra="ignore")

    start: str | None = Field(default=None, description="Дата/условие начала работ")
    end: str | None = Field(default=None, description="Дата/условие окончания работ")
    duration: str | None = Field(
        default=None, description='Длительность, например «60 календарных дней»'
    )
    stages: list[Stage] = Field(default_factory=list)
    raw_mentions: list[str] = Field(
        default_factory=list,
        description="Цитаты из документа, на которых основаны сроки",
    )


class Requirement(BaseModel):
    """Одно требование к участнику закупки / исполнителю."""

    model_config = ConfigDict(extra="ignore")

    category: RequirementCategory = RequirementCategory.OTHER
    text: str = Field(description="Формулировка требования, сжатая до одного пункта")
    mandatory: bool = Field(
        default=True, description="Обязательное (True) или желательное (False)"
    )
    source_pages: list[int] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def _fallback_category(cls, value: Any) -> Any:
        """Неизвестную категорию от LLM сводим к ``other``, а не роняем ответ."""
        if value is None:
            return RequirementCategory.OTHER
        if isinstance(value, RequirementCategory):
            return value
        normalized = str(value).strip().lower()
        allowed = {item.value for item in RequirementCategory}
        return normalized if normalized in allowed else RequirementCategory.OTHER


class Penalty(BaseModel):
    """Мера ответственности исполнителя."""

    model_config = ConfigDict(extra="ignore")

    violation: str = Field(description="За что наступает ответственность")
    sanction: str = Field(description="Что именно взыскивается: пеня, штраф, удержание")
    formula: str | None = Field(
        default=None,
        description='Порядок расчёта, например «0,1 % цены контракта за день»',
    )
    amount: Money | None = Field(
        default=None, description="Фиксированная сумма, если она указана"
    )
    source_pages: list[int] = Field(default_factory=list)


class TenderSummary(BaseModel):
    """Итоговая выжимка по документации."""

    model_config = ConfigDict(extra="ignore")

    subject: str | None = Field(default=None, description="Предмет закупки")
    customer: str | None = Field(default=None, description="Заказчик")
    procurement_number: str | None = Field(
        default=None, description="Номер извещения/закупки, если найден"
    )
    contract_price: Money | None = Field(
        default=None, description="Сумма контракта (НМЦК)"
    )
    contract_security: Money | None = Field(
        default=None, description="Обеспечение исполнения контракта"
    )
    application_security: Money | None = Field(
        default=None, description="Обеспечение заявки"
    )
    deadlines: Deadlines = Field(default_factory=Deadlines)
    requirements: list[Requirement] = Field(default_factory=list)
    penalties: list[Penalty] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description="Пропуски, сомнительные суммы и расхождения с правилами",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Оценка полноты и надёжности выжимки (0..1): заполненность ключевых "
            "разделов с поправкой на долю обработанных фрагментов и предупреждения"
        ),
    )


class DocumentMeta(BaseModel):
    """Метаданные обработанного файла."""

    filename: str
    size_bytes: int
    pages: int
    pages_with_text: int
    characters: int
    extractor: str = Field(description="Какой бэкенд вытащил текст: pdfplumber/pypdf")
    likely_scanned: bool = Field(
        default=False,
        description="Текстового слоя почти нет, вероятно, скан без OCR",
    )


class LLMUsage(BaseModel):
    """Что и сколько потратили на генерацию."""

    provider: str
    model: str
    chunks_total: int
    chunks_sent: int
    calls: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class SummarizeResponse(BaseModel):
    """Ответ ``POST /api/v1/summarize``."""

    request_id: str
    document: DocumentMeta
    llm: LLMUsage
    summary: TenderSummary
    elapsed_ms: int


class HealthResponse(BaseModel):
    status: str
    version: str
    provider: str
    model: str
    provider_ready: bool
    detail: str | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str
    request_id: str | None = None
