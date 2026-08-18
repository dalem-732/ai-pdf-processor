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
from dataclasses import dataclass

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
from app.core.heuristics import heuristic_summary
from app.core.merge import merge_summaries, refine_summary, score_confidence
from app.core.prompts import SYSTEM_PROMPT, build_extraction_prompt
from app.core.validation import verify_and_enrich
from app.errors import NoTextLayerError, ProviderUnavailableError, UnreadablePdfError
from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResult
from app.pdf.extract import PdfExtractionError, extract_text
from app.schemas import DocumentMeta, LLMUsage, TenderSummary

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str, str | None], Awaitable[None]]

# Три параллельных запроса - компромисс: облако при большем числе чаще
# отвечает 429, а локальная Ollama на одной GPU всё равно обрабатывает их по очереди.
MAX_CONCURRENT_LLM_CALLS = 3


@dataclass(slots=True)
class PipelineResult:
    document: DocumentMeta
    llm: LLMUsage
    summary: TenderSummary
    elapsed_ms: int


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
        # Разбор PDF - чистый CPU и десятки секунд на большом файле. В event
        # loop он бы заблокировал все параллельные запросы сервиса.
        document = await asyncio.to_thread(extract_text, content)
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
    selected = select_chunks(all_chunks, max_chunks=settings.max_chunks)
    marked_text = build_marked_text(document)

    await emit(
        18,
        "chunk",
        f"Отобрано {len(selected)} из {len(all_chunks)} фрагментов",
    )

    if client.name == "stub":
        logger.info(
            "request_id=%s страниц=%d символов=%d провайдер=stub (полный документ)",
            request_id,
            meta.pages,
            meta.characters,
        )
        await emit(25, "llm", "Анализ правилами")
        summary = await asyncio.to_thread(heuristic_summary, marked_text)
        await emit(70, "llm", "Правила отработали")
        await emit(75, "verify", "Сверка значений")
        summary = await asyncio.to_thread(verify_and_enrich, summary, marked_text)
        await emit(90, "finish", "Очистка выжимки")
        summary = refine_summary(summary)
        summary.confidence = score_confidence(
            summary,
            chunks_sent=len(all_chunks),
            chunks_total=len(all_chunks),
        )
        await emit(100, "finish", "Готово")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        llm_usage = LLMUsage(
            provider=client.name,
            model=client.model,
            chunks_total=len(all_chunks),
            chunks_sent=len(all_chunks),
            calls=0,
            prompt_tokens=None,
            completion_tokens=None,
        )
        logger.info(
            "request_id=%s готово за %d мс, confidence=%.2f, требований=%d, штрафов=%d",
            request_id,
            elapsed_ms,
            summary.confidence,
            len(summary.requirements),
            len(summary.penalties),
        )
        return PipelineResult(
            document=meta, llm=llm_usage, summary=summary, elapsed_ms=elapsed_ms
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

    partials, failures, usage_totals = await _run_extraction(
        client=client,
        chunks=selected,
        settings=settings,
        request_id=request_id,
        progress=progress,
    )

    if not partials:
        raise ProviderUnavailableError(
            "Ни один фрагмент документа не удалось обработать моделью: "
            + "; ".join(failures[:3])
        )

    await emit(88, "merge", "Слияние фрагментов")
    summary = merge_summaries(partials)
    if len(partials) < len(selected):
        summary.warnings.append(
            f"Обработано {len(partials)} из {len(selected)} фрагментов документа: "
            "часть запросов к модели завершилась ошибкой, выжимка может быть неполной"
        )
    if len(selected) < len(all_chunks):
        summary.warnings.append(
            f"В модель отправлено {len(selected)} из {len(all_chunks)} фрагментов "
            "(отобраны наиболее релевантные); увеличьте TENDER_MAX_CHUNKS для "
            "полного разбора"
        )

    # Сверка гоняет регулярки по всему документу - тоже CPU-bound.
    await emit(92, "verify", "Сверка с документом")
    summary = await asyncio.to_thread(verify_and_enrich, summary, marked_text)
    await emit(97, "finish", "Очистка выжимки")
    summary = refine_summary(summary)
    summary.confidence = score_confidence(
        summary,
        chunks_sent=len(selected),
        chunks_total=len(all_chunks),
    )
    await emit(100, "finish", "Готово")

    llm_usage = LLMUsage(
        provider=client.name,
        model=client.model,
        chunks_total=len(all_chunks),
        chunks_sent=len(selected),
        calls=len(partials) + len(failures),
        prompt_tokens=usage_totals.get("prompt"),
        completion_tokens=usage_totals.get("completion"),
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        "request_id=%s готово за %d мс, confidence=%.2f, требований=%d, штрафов=%d",
        request_id,
        elapsed_ms,
        summary.confidence,
        len(summary.requirements),
        len(summary.penalties),
    )
    return PipelineResult(
        document=meta, llm=llm_usage, summary=summary, elapsed_ms=elapsed_ms
    )


async def _run_extraction(
    *,
    client: LLMClient,
    chunks: list[Chunk],
    settings: Settings,
    request_id: str,
    progress: ProgressCallback | None = None,
) -> tuple[list[TenderSummary], list[str], dict[str, int | None]]:
    """Стадия map: параллельно извлечь факты из каждого фрагмента."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
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
                return None, f"{chunk.label}: {exc}", None

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

    partials: list[TenderSummary] = []
    failures: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    saw_tokens = False

    for summary, failure, result in outcomes:
        if summary is not None:
            partials.append(summary)
        if failure is not None:
            failures.append(failure)
        if result is not None:
            if result.prompt_tokens:
                prompt_tokens += result.prompt_tokens
                saw_tokens = True
            if result.completion_tokens:
                completion_tokens += result.completion_tokens
                saw_tokens = True

    usage = {
        "prompt": prompt_tokens if saw_tokens else None,
        "completion": completion_tokens if saw_tokens else None,
    }
    return partials, failures, usage


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
