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
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import LLMProvider, Settings, get_settings
from app.core.pipeline import ProgressCallback, summarize_pdf
from app.errors import (
    AppError,
    FileTooLargeError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
    UnsupportedFileError,
)
from app.llm import build_client
from app.llm.base import LLMError
from app.schemas import ErrorResponse, HealthResponse, SummarizeResponse
from app.web import UPLOAD_PAGE_HTML

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
ALLOWED_CONTENT_TYPES = frozenset(
    {"application/pdf", "application/x-pdf", "application/octet-stream", ""}
)
# Читаем загрузку порциями, чтобы отклонить слишком большой файл, не подняв
# его целиком в память.
UPLOAD_READ_CHUNK = 1024 * 1024


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

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        payload = ErrorResponse(
            error=exc.code,
            detail=exc.detail,
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump())

    @app.exception_handler(LLMError)
    async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
        payload = ErrorResponse(
            error="provider_error",
            detail=str(exc),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=502, content=payload.model_dump())

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
        )

    @app.post(
        "/api/v1/summarize",
        response_model=SummarizeResponse,
        tags=["summarize"],
        responses={
            413: {"model": ErrorResponse, "description": "Файл превышает лимит"},
            415: {"model": ErrorResponse, "description": "Ожидается PDF"},
            422: {
                "model": ErrorResponse,
                "description": "PDF не читается или в нём нет текстового слоя",
            },
            502: {"model": ErrorResponse, "description": "Провайдер LLM недоступен"},
            503: {"model": ErrorResponse, "description": "Провайдер LLM не настроен"},
        },
    )
    async def summarize(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        file: Annotated[UploadFile, File(description="PDF тендерной документации")],
        provider: Annotated[
            LLMProvider | None,
            Form(description="Переопределить провайдера LLM для этого запроса"),
        ] = None,
        max_chunks: Annotated[
            int | None,
            Form(ge=1, le=200, description="Ограничение числа фрагментов документа"),
        ] = None,
    ) -> SummarizeResponse:
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        _validate_upload_metadata(file)
        effective = _effective_settings(settings, provider, max_chunks)
        content = await _read_upload(file, effective.max_upload_bytes)
        return await _execute_summarize(
            content=content,
            filename=file.filename or "document.pdf",
            effective=effective,
            request_id=request_id,
        )

    @app.post(
        "/api/v1/summarize/stream",
        tags=["summarize"],
        include_in_schema=True,
        responses={
            413: {"model": ErrorResponse, "description": "Файл превышает лимит"},
            415: {"model": ErrorResponse, "description": "Ожидается PDF"},
            422: {
                "model": ErrorResponse,
                "description": "PDF не читается или в нём нет текстового слоя",
            },
            502: {"model": ErrorResponse, "description": "Провайдер LLM недоступен"},
            503: {"model": ErrorResponse, "description": "Провайдер LLM не настроен"},
        },
    )
    async def summarize_stream(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        file: Annotated[UploadFile, File(description="PDF тендерной документации")],
        provider: Annotated[
            LLMProvider | None,
            Form(description="Переопределить провайдера LLM для этого запроса"),
        ] = None,
        max_chunks: Annotated[
            int | None,
            Form(ge=1, le=200, description="Ограничение числа фрагментов документа"),
        ] = None,
    ) -> StreamingResponse:
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        _validate_upload_metadata(file)
        effective = _effective_settings(settings, provider, max_chunks)
        content = await _read_upload(file, effective.max_upload_bytes)

        async def event_stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

            async def on_progress(
                percent: int, stage: str, detail: str | None = None
            ) -> None:
                await queue.put(
                    {
                        "type": "progress",
                        "percent": percent,
                        "stage": stage,
                        "detail": detail,
                    }
                )

            async def run() -> None:
                try:
                    payload = await _execute_summarize(
                        content=content,
                        filename=file.filename or "document.pdf",
                        effective=effective,
                        request_id=request_id,
                        progress=on_progress,
                    )
                    await queue.put(
                        {"type": "done", "payload": payload.model_dump(mode="json")}
                    )
                except AppError as exc:
                    await queue.put(
                        {
                            "type": "error",
                            "status": exc.status_code,
                            "error": exc.code,
                            "detail": exc.detail,
                            "request_id": request_id,
                        }
                    )
                except LLMError as exc:
                    await queue.put(
                        {
                            "type": "error",
                            "status": 502,
                            "error": "provider_error",
                            "detail": str(exc),
                            "request_id": request_id,
                        }
                    )
                finally:
                    await queue.put(None)

            task = asyncio.create_task(run())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                await task

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _effective_settings(
    settings: Settings,
    provider: LLMProvider | None,
    max_chunks: int | None,
) -> Settings:
    overrides: dict[str, object] = {}
    if provider is not None:
        overrides["llm_provider"] = provider
    if max_chunks is not None:
        overrides["max_chunks"] = max_chunks
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
        raise ProviderNotConfiguredError(str(exc)) from exc

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
        raise ProviderUnavailableError(str(exc)) from exc

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
