"""Пайплайн: PDF на входе - структурированная выжимка на выходе.

Последовательность шагов и причины, по которым она такая:

1. **Извлечение текста.** Разбор PDF с таблицами, чистка колонтитулов,
   разметка страниц маркерами ``[[стр. N]]`` для прослеживаемости фактов.
2. **Нарезка на фрагменты.** Документация с ЕИС не влезает ни в контекст
   локальной модели, ни в разумный бюджет облачной LLM. При нарезке
   предпочитаются границы страниц и абзацев, соседние фрагменты перекрываются.
3. **Отбор фрагментов.** По ключевым словам закупочной терминологии
   выбираются наиболее вероятные носители цены, сроков, требований и штрафов;
   первый фрагмент включается всегда - там титульные данные.
4. **Map: параллельное извлечение.** Каждый фрагмент уходит в LLM отдельным
   запросом с требованием строгого JSON. Запросы идут параллельно с
   ограничением параллелизма, чтобы не получить 429 на облаке и не
   положить локальную Ollama.
5. **Reduce: детерминированное слияние.** Части сливаются кодом, а не второй
   генерацией - LLM на слиянии теряет пункты и добавляет свои.
6. **Сверка и дополнение.** Суммы из ответа модели проверяются по исходному
   тексту; пустые поля восстанавливаются правилами; важные расхождения
   попадают в warnings.
7. **Очистка выжимки.** Административные сроки убираются из этапов работ,
   неденежные санкции - из штрафов, рутинные обязанности - в конец списка
   требований.
8. **Оценка полноты.** confidence учитывает заполненность разделов, долю
   обработанных фрагментов и предупреждения сверки.

Устойчивость обеспечивается тем, что отказ на одном фрагменте не отменяет
результат: сбойный фрагмент превращается в предупреждение, а ошибкой запроса
становится только полный провал всех фрагментов.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.core.chunking import Chunk, build_chunks, build_marked_text, select_chunks
from app.core.json_utils import JsonParseError, parse_json_object
from app.core.merge import merge_summaries, refine_summary, score_confidence
from app.core.prompts import SYSTEM_PROMPT, build_extraction_prompt
from app.core.validation import verify_and_enrich
from app.errors import (
    NoTextLayerError,
    PdfTooComplexError,
    ProviderUnavailableError,
    TooManyPagesError,
    UnreadablePdfError,
)
from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult
from app.pdf.extract import PdfExtractionError, PdfPageLimitError
from app.pdf.isolation import PdfResourceError, PdfTimeoutError, extract_text_isolated
from app.schemas import DocumentMeta, LLMUsage, TenderSummary

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str, str | None], Awaitable[None]]


@dataclass(slots=True)
class PipelineResult:
    document: DocumentMeta
    llm: LLMUsage
    summary: TenderSummary
    elapsed_ms: int


@dataclass(slots=True)
class _ExtractionOutcome:
    partials: list[TenderSummary] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    truncated: int = 0


async def summarize_pdf(
    *,
    content: bytes,
    filename: str,
    settings: Settings,
    client: LLMClient,
    request_id: str,
    progress: ProgressCallback | None = None,
) -> PipelineResult:
    started = time.perf_counter()

    async def emit(percent: int, stage: str, detail: str | None = None) -> None:
        if progress is not None:
            await progress(min(100, max(0, percent)), stage, detail)

    await emit(2, "extract", "Чтение PDF")

    try:
        document = await extract_text_isolated(
            content,
            max_pages=settings.max_pages,
            timeout_seconds=settings.pdf_parse_timeout_seconds,
            memory_mb=settings.pdf_parse_memory_mb,
        )
    except PdfPageLimitError as exc:
        raise TooManyPagesError(str(exc)) from exc
    except (PdfTimeoutError, PdfResourceError) as exc:
        raise PdfTooComplexError(str(exc)) from exc
    except PdfExtractionError as exc:
        raise UnreadablePdfError(str(exc)) from exc

    await emit(
        12,
        "extract",
        f"{len(document.pages)} стр., {document.characters} симв.",
    )

    if document.likely_scanned:
        raise NoTextLayerError(
            "В PDF нет текстового слоя (вероятно, это скан). Распознайте файл "
            "OCR-инструментом (ocrmypdf, ABBYY FineReader) и повторите загрузку."
        )

    meta = DocumentMeta(
        filename=filename,
        size_bytes=len(content),
        pages=len(document.pages),
        pages_with_text=document.pages_with_text,
        characters=document.characters,
        extractor=document.extractor,
        likely_scanned=document.likely_scanned,
    )

    all_chunks = build_chunks(
        document,
        chunk_chars=settings.chunk_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    marked_text = build_marked_text(document)
    selected = _apply_char_budget(
        select_chunks(all_chunks, max_chunks=settings.max_chunks),
        settings.max_llm_input_chars,
    )
    await emit(
        18,
        "chunk",
        f"Отобрано {len(selected)} из {len(all_chunks)} фрагментов",
    )

    direct = await asyncio.to_thread(client.summarize_document, marked_text)
    if direct is not None:
        logger.info(
            "request_id=%s страниц=%d символов=%d провайдер=%s (полный документ)",
            request_id,
            meta.pages,
            meta.characters,
            client.name,
        )
        await emit(70, "llm", "Правила отработали")
        usage = LLMUsage(
            provider=client.name,
            model=client.model,
            chunks_total=len(all_chunks),
            chunks_sent=len(all_chunks),
            calls=0,
        )
        return await _finalize(
            direct,
            marked_text=marked_text,
            meta=meta,
            usage=usage,
            request_id=request_id,
            started=started,
            emit=emit,
        )

    logger.info(
        "request_id=%s страниц=%d символов=%d фрагментов=%d отправляем=%d провайдер=%s",
        request_id,
        meta.pages,
        meta.characters,
        len(all_chunks),
        len(selected),
        client.name,
    )

    outcome = await _run_extraction(
        client=client,
        chunks=selected,
        settings=settings,
        request_id=request_id,
        progress=progress,
    )

    if not outcome.partials:
        raise ProviderUnavailableError(
            "Ни один фрагмент документа не удалось обработать моделью: "
            + "; ".join(outcome.failures[:3])
            + f". Подробности в логах сервера (request_id={request_id})"
        )

    await emit(88, "merge", "Слияние фрагментов")
    summary = merge_summaries(outcome.partials)
    if len(outcome.partials) < len(selected):
        summary.warnings.append(
            f"Обработано {len(outcome.partials)} из {len(selected)} фрагментов документа: "
            "часть запросов к модели завершилась ошибкой, выжимка может быть неполной"
        )
    if len(selected) < len(all_chunks):
        summary.warnings.append(
            f"В модель отправлено {len(selected)} из {len(all_chunks)} фрагментов "
            "(отобраны наиболее релевантные); увеличьте TENDER_MAX_CHUNKS или "
            "TENDER_MAX_LLM_INPUT_CHARS для полного разбора"
        )
    if outcome.truncated:
        summary.warnings.append(
            f"Ответ модели оборвался на лимите выходных токенов во фрагментах: "
            f"{outcome.truncated}; часть фактов могла потеряться, увеличьте "
            "TENDER_LLM_MAX_OUTPUT_TOKENS"
        )

    usage = LLMUsage(
        provider=client.name,
        model=client.model,
        chunks_total=len(all_chunks),
        chunks_sent=len(selected),
        calls=len(outcome.partials) + len(outcome.failures),
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        truncated_responses=outcome.truncated,
    )
    return await _finalize(
        summary,
        marked_text=marked_text,
        meta=meta,
        usage=usage,
        request_id=request_id,
        started=started,
        emit=emit,
    )


async def _finalize(
    summary: TenderSummary,
    *,
    marked_text: str,
    meta: DocumentMeta,
    usage: LLMUsage,
    request_id: str,
    started: float,
    emit: Callable[[int, str, str | None], Awaitable[None]],
) -> PipelineResult:
    await emit(92, "verify", "Сверка с документом")
    summary = await asyncio.to_thread(verify_and_enrich, summary, marked_text)
    await emit(97, "finish", "Очистка выжимки")
    summary = refine_summary(summary)
    summary.confidence = score_confidence(
        summary,
        chunks_sent=usage.chunks_sent,
        chunks_total=usage.chunks_total,
    )
    await emit(100, "finish", "Готово")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "request_id=%s готово за %d мс, confidence=%.2f, требований=%d, штрафов=%d",
        request_id,
        elapsed_ms,
        summary.confidence,
        len(summary.requirements),
        len(summary.penalties),
    )
    return PipelineResult(document=meta, llm=usage, summary=summary, elapsed_ms=elapsed_ms)


def _apply_char_budget(chunks: list[Chunk], budget: int) -> list[Chunk]:
    """Ограничить суммарный объём текста, уходящего в модель за один запрос."""
    if not chunks:
        return chunks
    ranked = [chunks[0]] + sorted(chunks[1:], key=lambda chunk: chunk.score, reverse=True)
    kept: list[Chunk] = []
    used = 0
    for chunk in ranked:
        if kept and used + len(chunk.text) > budget:
            continue
        kept.append(chunk)
        used += len(chunk.text)
    return sorted(kept, key=lambda chunk: chunk.index)


async def _run_extraction(
    *,
    client: LLMClient,
    chunks: list[Chunk],
    settings: Settings,
    request_id: str,
    progress: ProgressCallback | None = None,
) -> _ExtractionOutcome:
    """Стадия map: параллельно извлечь факты из каждого фрагмента."""
    semaphore = asyncio.Semaphore(settings.llm_concurrency)
    progress_lock = asyncio.Lock()
    completed_chunks = 0
    llm_start = 20
    llm_end = 85
    total_chunks = len(chunks)

    async def report_chunk_done(chunk: Chunk) -> None:
        nonlocal completed_chunks
        if progress is None or total_chunks == 0:
            return
        async with progress_lock:
            completed_chunks += 1
            percent = llm_start + int(
                (llm_end - llm_start) * completed_chunks / total_chunks
            )
            await progress(
                percent,
                "llm",
                f"Фрагмент {completed_chunks} из {total_chunks} ({chunk.label})",
            )

    async def worker(chunk: Chunk) -> tuple[TenderSummary | None, str | None, LLMResult | None]:
        request = LLMRequest(
            system=SYSTEM_PROMPT,
            user=build_extraction_prompt(
                chunk_text=chunk.text,
                part_number=chunk.index + 1,
                parts_total=len(chunks),
                pages_label=chunk.label,
            ),
            document_text=chunk.text,
        )
        async with semaphore:
            try:
                result = await _complete_with_retries(client, request, settings)
            except LLMError as exc:
                logger.warning(
                    "request_id=%s фрагмент %s: ошибка модели: %s",
                    request_id,
                    chunk.label,
                    exc,
                )
                await report_chunk_done(chunk)
                return None, f"{chunk.label}: {exc.public_message}", None

        try:
            payload = parse_json_object(result.text)
            summary = TenderSummary.model_validate(payload)
        except (JsonParseError, ValidationError) as exc:
            logger.warning(
                "request_id=%s фрагмент %s: ответ модели не валиден: %s",
                request_id,
                chunk.label,
                exc,
            )
            await report_chunk_done(chunk)
            return None, f"{chunk.label}: ответ модели не соответствует схеме", result
        await report_chunk_done(chunk)
        return summary, None, result

    outcomes = await asyncio.gather(*(worker(chunk) for chunk in chunks))

    outcome = _ExtractionOutcome()
    for summary, failure, result in outcomes:
        if summary is not None:
            outcome.partials.append(summary)
        if failure is not None:
            outcome.failures.append(failure)
        if result is None:
            continue
        if result.truncated:
            outcome.truncated += 1
        if result.prompt_tokens:
            outcome.prompt_tokens = (outcome.prompt_tokens or 0) + result.prompt_tokens
        if result.completion_tokens:
            outcome.completion_tokens = (
                outcome.completion_tokens or 0
            ) + result.completion_tokens
    return outcome


async def _complete_with_retries(
    client: LLMClient, request: LLMRequest, settings: Settings
) -> LLMResult:
    """Вызов модели с повтором только по восстановимым ошибкам.

    Повторять 401 или "модель не найдена" бессмысленно - запрос не изменится,
    а пользователь ждёт лишние секунды на каждый фрагмент.
    """
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(
            lambda exc: isinstance(exc, LLMError) and exc.retryable
        ),
        stop=stop_after_attempt(settings.llm_max_retries + 1),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        reraise=True,
    ):
        with attempt:
            return await client.complete(request)
    raise ProviderUnavailableError("Не удалось получить ответ модели")  # pragma: no cover
