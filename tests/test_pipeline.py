"""Тесты пайплайна: устойчивость к сбоям модели и корректность map-reduce.

Вместо реальной модели подставляется управляемый клиент: только так можно
воспроизвести таймаут на одном фрагменте, невалидный JSON на другом и
проверить, что сервис всё равно отдаёт результат.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.core.pipeline import summarize_pdf
from app.errors import NoTextLayerError, ProviderUnavailableError, UnreadablePdfError
from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult

COMMON = {"timeout": 5.0, "temperature": 0.0, "max_output_tokens": 512}


class ScriptedClient(LLMClient):
    """Клиент, отвечающий по заранее заданному сценарию на каждый вызов."""

    name = "scripted"

    def __init__(self, script: list[object]) -> None:
        super().__init__(model="scripted-model", **COMMON)
        self._script = list(script)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        item = self._script.pop(0) if self._script else '{"requirements": []}'
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=item, prompt_tokens=100, completion_tokens=50)


def _payload(**overrides: object) -> str:
    data = {
        "subject": "Поставка оборудования",
        "customer": "ГБУЗ № 3",
        "contract_price": {"amount": 12480500.0, "currency": "RUB", "raw": "12 480 500,00 рублей"},
        "deadlines": {"end": "31.08.2026", "duration": "180 календарных дней", "stages": []},
        "requirements": [{"category": "license", "text": "Наличие лицензии", "mandatory": True}],
        "penalties": [{"violation": "Просрочка", "sanction": "Пеня", "formula": "0,1 %"}],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


async def test_stub_provider_uses_full_document_heuristics(sample_pdf_bytes: bytes) -> None:
    from app.llm.stub_client import StubClient

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=Settings(llm_provider="stub"),
        client=StubClient(model="heuristic-stub", **COMMON),
        request_id="test-stub-full",
    )

    assert result.llm.calls == 0
    assert result.llm.chunks_sent == result.llm.chunks_total
    assert result.summary.contract_price is not None


async def test_happy_path_returns_full_summary(sample_pdf_bytes: bytes) -> None:
    client = ScriptedClient([_payload()])
    settings = Settings(llm_provider="stub")

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=settings,
        client=client,
        request_id="test-happy",
    )

    assert result.summary.contract_price.amount == pytest.approx(12_480_500.0)
    assert result.summary.requirements and result.summary.penalties
    assert result.summary.confidence == pytest.approx(1.0)
    assert result.document.pages >= 1
    assert result.llm.prompt_tokens == 100
    assert result.elapsed_ms >= 0


async def test_one_failed_chunk_does_not_lose_the_document(sample_pdf_bytes: bytes) -> None:
    """Таймаут на одном фрагменте - предупреждение, а не потеря результата."""
    client = ScriptedClient([_payload(), LLMError("Таймаут", retryable=False)])
    settings = Settings(llm_provider="stub", chunk_chars=1_500, chunk_overlap_chars=100, max_chunks=2)

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=settings,
        client=client,
        request_id="test-partial",
    )

    assert result.summary.contract_price is not None
    assert any("Обработано 1 из 2 фрагментов" in w for w in result.summary.warnings)


async def test_invalid_json_chunk_is_reported_but_tolerated(sample_pdf_bytes: bytes) -> None:
    client = ScriptedClient(["Извините, не могу помочь", _payload()])
    settings = Settings(llm_provider="stub", chunk_chars=1_500, chunk_overlap_chars=100, max_chunks=2)

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=settings,
        client=client,
        request_id="test-badjson",
    )

    assert result.summary.contract_price is not None
    assert any("фрагментов" in warning for warning in result.summary.warnings)


async def test_all_chunks_failed_raises_provider_unavailable(sample_pdf_bytes: bytes) -> None:
    client = ScriptedClient([LLMError("HTTP 401", retryable=False)])
    settings = Settings(llm_provider="stub")

    with pytest.raises(ProviderUnavailableError, match="Ни один фрагмент"):
        await summarize_pdf(
            content=sample_pdf_bytes,
            filename="tender.pdf",
            settings=settings,
            client=client,
            request_id="test-alldead",
        )


async def test_retryable_error_is_retried(sample_pdf_bytes: bytes) -> None:
    client = ScriptedClient([LLMError("429 Too Many Requests", retryable=True), _payload()])
    settings = Settings(llm_provider="stub", llm_max_retries=1)

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=settings,
        client=client,
        request_id="test-retry",
    )

    assert client.calls == 2
    assert result.summary.contract_price is not None


async def test_non_retryable_error_is_not_retried(sample_pdf_bytes: bytes) -> None:
    """401 повторять бессмысленно: запрос не изменится, а клиент ждёт."""
    client = ScriptedClient([LLMError("401 Unauthorized", retryable=False)])
    settings = Settings(llm_provider="stub", llm_max_retries=3)

    with pytest.raises(ProviderUnavailableError):
        await summarize_pdf(
            content=sample_pdf_bytes,
            filename="tender.pdf",
            settings=settings,
            client=client,
            request_id="test-noretry",
        )

    assert client.calls == 1


async def test_model_silence_is_compensated_by_rules(sample_pdf_bytes: bytes) -> None:
    """Модель вернула пустую выжимку, факты дополняются правилами."""
    client = ScriptedClient(["{}"])
    settings = Settings(llm_provider="stub")

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=settings,
        client=client,
        request_id="test-empty",
    )

    assert result.summary.contract_price.amount == pytest.approx(12_480_500.0)
    assert result.summary.requirements
    assert result.summary.penalties
    assert result.summary.warnings


async def test_scanned_pdf_rejected_with_ocr_hint() -> None:
    """PDF без текстового слоя: отказ с рекомендацией, а не пустая выжимка."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(NoTextLayerError, match="OCR"):
        await summarize_pdf(
            content=buffer.getvalue(),
            filename="scan.pdf",
            settings=Settings(llm_provider="stub"),
            client=ScriptedClient([_payload()]),
            request_id="test-scan",
        )


async def test_broken_file_reported_as_unreadable() -> None:
    with pytest.raises(UnreadablePdfError):
        await summarize_pdf(
            content=b"%PDF-1.4 not really a pdf",
            filename="broken.pdf",
            settings=Settings(llm_provider="stub"),
            client=ScriptedClient([_payload()]),
            request_id="test-broken",
        )


async def test_chunk_limit_is_reported_in_warnings(sample_pdf_bytes: bytes) -> None:
    client = ScriptedClient([_payload(), _payload()])
    settings = Settings(llm_provider="stub", chunk_chars=1_000, chunk_overlap_chars=0, max_chunks=2)

    result = await summarize_pdf(
        content=sample_pdf_bytes,
        filename="tender.pdf",
        settings=settings,
        client=client,
        request_id="test-limit",
    )

    assert result.llm.chunks_sent == 2
    assert result.llm.chunks_total > 2
    assert any("отобраны наиболее релевантные" in w for w in result.summary.warnings)
