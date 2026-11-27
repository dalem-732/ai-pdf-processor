"""HTTP-слой сервиса суммаризации тендерной документации.

Endpoints:

* ``POST /api/v1/summarize`` - принимает PDF, возвращает структурированную
  выжимку: сумма контракта, сроки, требования к исполнителю, штрафы;
* ``POST /api/v1/summarize/stream`` - то же, но с SSE-событиями прогресса;
* ``GET /api/v1/health`` - состояние сервиса и готовность LLM-провайдера;
* ``GET /`` - минимальная страница для загрузки файла руками;
* ``GET /docs`` - интерактивная документация OpenAPI (генерируется FastAPI).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import __version__
from app.config import LLMProvider, Settings, get_settings
from app.core.pipeline import ProgressCallback, summarize_pdf
from app.errors import (
    AppError,
    FileTooLargeError,
    ProviderNotConfiguredError,
    ProviderOverrideForbiddenError,
    ProviderUnavailableError,
    ServerBusyError,
    UnsupportedFileError,
)
from app.llm import build_client
from app.llm.base import LLMError
from app.middleware import (
    RequestGuardMiddleware,
    content_security_policy,
    new_request_id,
    script_hash,
)
from app.schemas import ErrorResponse, HealthResponse, SummarizeResponse
from app.web import UPLOAD_PAGE_HTML

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = frozenset(
    {"application/pdf", "application/x-pdf", "application/octet-stream", ""}
)
UPLOAD_READ_CHUNK = 1024 * 1024
INTERNAL_ERROR_DETAIL = "Внутренняя ошибка сервиса. Подробности в логах сервера по request_id"

SUMMARIZE_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Нужен API-ключ (если задан TENDER_API_KEY)"},
    403: {"model": ErrorResponse, "description": "Переопределение провайдера запрещено"},
    413: {"model": ErrorResponse, "description": "Файл или число страниц превышают лимит"},
    415: {"model": ErrorResponse, "description": "Ожидается PDF"},
    422: {
        "model": ErrorResponse,
        "description": "PDF не читается, слишком сложный или в нём нет текстового слоя",
    },
    429: {"model": ErrorResponse, "description": "Превышен лимит запросов"},
    502: {"model": ErrorResponse, "description": "Провайдер LLM недоступен"},
    503: {"model": ErrorResponse, "description": "Провайдер не настроен или сервис занят"},
}


@dataclass(slots=True)
class SummarizeForm:
    file: UploadFile
    provider: LLMProvider | None
    max_chunks: int | None


def summarize_form(
    file: Annotated[UploadFile, File(description="PDF тендерной документации")],
    provider: Annotated[
        LLMProvider | None,
        Form(
            description=(
                "Переопределить провайдера LLM для этого запроса "
                "(только при TENDER_ALLOW_PROVIDER_OVERRIDE=true)"
            )
        ),
    ] = None,
    max_chunks: Annotated[
        int | None,
        Form(
            ge=1,
            le=200,
            description="Ограничение числа фрагментов (не больше TENDER_MAX_CHUNKS)",
        ),
    ] = None,
) -> SummarizeForm:
    return SummarizeForm(file=file, provider=provider, max_chunks=max_chunks)


class JobSlots:
    """Неблокирующий лимит одновременно обрабатываемых документов."""

    def __init__(self) -> None:
        self.active = 0

    def acquire(self, limit: int) -> Callable[[], None]:
        if self.active >= limit:
            raise ServerBusyError(
                "Сервис обрабатывает максимум документов одновременно, повторите позже"
            )
        self.active += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.active -= 1

        return release


@dataclass(slots=True)
class SummarizeJob:
    content: bytes
    filename: str
    settings: Settings
    request_id: str

    async def run(self, progress: ProgressCallback | None = None) -> SummarizeResponse:
        return await _execute_summarize(
            content=self.content,
            filename=self.filename,
            effective=self.settings,
            request_id=self.request_id,
            progress=progress,
        )


def _resolve_settings(app: FastAPI) -> Settings:
    return app.dependency_overrides.get(get_settings, get_settings)()


def _page_script() -> str:
    match = re.search(r"<script>(.*?)</script>", UPLOAD_PAGE_HTML, re.DOTALL)
    return match.group(1) if match else ""


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or new_request_id()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="Суммаризатор тендерной документации",
        version=__version__,
        summary=(
            "Извлекает из PDF с госзакупок сумму контракта, сроки выполнения, "
            "требования к исполнителю и перечень штрафов"
        ),
        description=(
            "Сервис разбирает PDF тендерной документации, нарезает текст на "
            "фрагменты, извлекает факты через LLM (OpenAI, Anthropic или "
            "локальная модель в Ollama), сверяет числовые значения с исходным "
            "текстом и возвращает структурированный JSON."
        ),
    )
    app.state.jobs = JobSlots()
    app.add_middleware(
        RequestGuardMiddleware,
        settings_provider=lambda: _resolve_settings(app),
        page_csp=content_security_policy([script_hash(_page_script())]),
    )

    def error_response(request: Request, status: int, code: str, detail: str) -> JSONResponse:
        payload = ErrorResponse(error=code, detail=detail, request_id=_request_id(request))
        return JSONResponse(status_code=status, content=payload.model_dump())

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.detail)

    @app.exception_handler(LLMError)
    async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
        logger.warning("request_id=%s ошибка провайдера: %s", _request_id(request), exc)
        return error_response(request, 502, "provider_error", exc.public_message)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("request_id=%s необработанная ошибка", _request_id(request))
        return error_response(request, 500, "internal_error", INTERNAL_ERROR_DETAIL)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(UPLOAD_PAGE_HTML)

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["service"])
    async def health(
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> HealthResponse:
        problem = settings.missing_credentials()
        return HealthResponse(
            status="ok",
            version=__version__,
            provider=settings.llm_provider.value,
            model=settings.active_model,
            provider_ready=problem is None,
            detail=problem,
            auth_required=settings.auth_required,
            provider_override_allowed=settings.allow_provider_override,
            max_upload_mb=settings.max_upload_mb,
        )

    @app.post(
        "/api/v1/summarize",
        response_model=SummarizeResponse,
        tags=["summarize"],
        responses=SUMMARIZE_RESPONSES,
    )
    async def summarize(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        form: Annotated[SummarizeForm, Depends(summarize_form)],
    ) -> SummarizeResponse:
        job = await _prepare_job(request, settings, form)
        release = request.app.state.jobs.acquire(job.settings.max_concurrent_jobs)
        try:
            return await job.run()
        finally:
            release()

    @app.post(
        "/api/v1/summarize/stream",
        tags=["summarize"],
        responses=SUMMARIZE_RESPONSES,
    )
    async def summarize_stream(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        form: Annotated[SummarizeForm, Depends(summarize_form)],
    ) -> StreamingResponse:
        job = await _prepare_job(request, settings, form)
        release = request.app.state.jobs.acquire(job.settings.max_concurrent_jobs)
        return StreamingResponse(
            stream_events(job.run, job.request_id, release),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(release),
        )

    return app


async def stream_events(
    runner: Callable[[ProgressCallback], Awaitable[SummarizeResponse]],
    request_id: str,
    release: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """SSE-события прогресса; при обрыве соединения работа отменяется."""
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

    async def on_progress(percent: int, stage: str, detail: str | None = None) -> None:
        await queue.put({"type": "progress", "percent": percent, "stage": stage, "detail": detail})

    def error_event(status: int, code: str, detail: str) -> dict[str, object]:
        return {
            "type": "error",
            "status": status,
            "error": code,
            "detail": detail,
            "request_id": request_id,
        }

    async def run() -> None:
        try:
            payload = await runner(on_progress)
            await queue.put({"type": "done", "payload": payload.model_dump(mode="json")})
        except AppError as exc:
            await queue.put(error_event(exc.status_code, exc.code, exc.detail))
        except LLMError as exc:
            logger.warning("request_id=%s ошибка провайдера: %s", request_id, exc)
            await queue.put(error_event(502, "provider_error", exc.public_message))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("request_id=%s необработанная ошибка", request_id)
            await queue.put(error_event(500, "internal_error", INTERNAL_ERROR_DETAIL))
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
    finally:
        if not task.done():
            task.cancel()
            logger.info("request_id=%s клиент отключился, обработка отменена", request_id)
        with contextlib.suppress(BaseException):
            await task
        if release is not None:
            release()


async def _prepare_job(request: Request, settings: Settings, form: SummarizeForm) -> SummarizeJob:
    _validate_upload_metadata(form.file)
    effective = _effective_settings(settings, form.provider, form.max_chunks)
    content = await _read_upload(form.file, effective.max_upload_bytes)
    return SummarizeJob(
        content=content,
        filename=form.file.filename or "document.pdf",
        settings=effective,
        request_id=_request_id(request),
    )


def _effective_settings(
    settings: Settings,
    provider: LLMProvider | None,
    max_chunks: int | None,
) -> Settings:
    overrides: dict[str, object] = {}
    if provider is not None and provider is not settings.llm_provider:
        if not settings.allow_provider_override:
            raise ProviderOverrideForbiddenError(
                "Переопределение провайдера отключено на сервере "
                "(TENDER_ALLOW_PROVIDER_OVERRIDE=false)"
            )
        overrides["llm_provider"] = provider
    if max_chunks is not None:
        overrides["max_chunks"] = min(max_chunks, settings.max_chunks)
    if not overrides:
        return settings
    return settings.model_copy(update=overrides)


async def _execute_summarize(
    *,
    content: bytes,
    filename: str,
    effective: Settings,
    request_id: str,
    progress: ProgressCallback | None = None,
) -> SummarizeResponse:
    problem = effective.missing_credentials()
    if problem:
        raise ProviderNotConfiguredError(
            f"Провайдер LLM не готов к работе: {problem}"
        )

    try:
        client = build_client(effective)
    except LLMError as exc:
        raise ProviderNotConfiguredError(exc.public_message) from exc

    try:
        result = await summarize_pdf(
            content=content,
            filename=filename,
            settings=effective,
            client=client,
            request_id=request_id,
            progress=progress,
        )
    except LLMError as exc:
        logger.warning("request_id=%s ошибка провайдера: %s", request_id, exc)
        raise ProviderUnavailableError(exc.public_message) from exc
    finally:
        await client.aclose()

    return SummarizeResponse(
        request_id=request_id,
        document=result.document,
        llm=result.llm,
        summary=result.summary,
        elapsed_ms=result.elapsed_ms,
    )


def _validate_upload_metadata(file: UploadFile) -> None:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    # Браузеры и curl присылают то application/pdf, то octet-stream, поэтому
    # достаточно, чтобы совпало либо имя, либо тип; окончательную проверку
    # делает сигнатура %PDF- в парсере.
    if content_type not in ALLOWED_CONTENT_TYPES and not filename.endswith(".pdf"):
        raise UnsupportedFileError(
            f"Ожидается PDF-файл, получен тип {file.content_type!r}"
        )
    if filename and not filename.endswith(".pdf") and content_type == "":
        raise UnsupportedFileError("Ожидается файл с расширением .pdf")


async def _read_upload(file: UploadFile, limit_bytes: int) -> bytes:
    """Прочитать файл порциями, оборвав чтение при превышении лимита."""
    buffer = bytearray()
    while chunk := await file.read(UPLOAD_READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > limit_bytes:
            raise FileTooLargeError(
                f"Файл больше допустимых {limit_bytes // (1024 * 1024)} МБ"
            )
    if not buffer:
        raise UnsupportedFileError("Загружен пустой файл")
    return bytes(buffer)


app = create_app()
