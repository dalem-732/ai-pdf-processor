"""Разбор PDF в отдельном процессе с ограничением времени и памяти."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from app.pdf.extract import ExtractedDocument, Page, PdfExtractionError, PdfPageLimitError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
_CRASH_DETAIL = "Разбор PDF аварийно завершился: файл повреждён или требует слишком много памяти"


class PdfTimeoutError(PdfExtractionError):
    """Разбор PDF не уложился в отведённое время."""


class PdfResourceError(PdfExtractionError):
    """Процесс разбора упал или превысил лимит памяти."""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = _PROJECT_ROOT + (os.pathsep + existing if existing else "")
    return env


async def extract_text_isolated(
    data: bytes,
    *,
    max_pages: int,
    timeout_seconds: float,
    memory_mb: int,
) -> ExtractedDocument:
    """Разобрать PDF в дочернем процессе и убить его при зависании или отмене."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.pdf.worker",
        str(max_pages),
        str(memory_mb),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_PROJECT_ROOT,
        env=_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise PdfTimeoutError(
            f"Разбор PDF не уложился в {timeout_seconds:.0f} с: документ слишком "
            "сложный или повреждён"
        ) from exc
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(process.wait())

    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "Процесс разбора PDF завершился с кодом %s: %s",
            process.returncode,
            stderr.decode("utf-8", "replace")[-500:],
        )
        raise PdfResourceError(_CRASH_DETAIL) from exc

    status = result.get("status")
    if status == "ok":
        return ExtractedDocument(
            pages=[Page(number=page["number"], text=page["text"]) for page in result["pages"]],
            extractor=result["extractor"],
        )
    if status == "pages":
        raise PdfPageLimitError(result["detail"])
    if status == "memory":
        raise PdfResourceError("Разбор PDF превысил лимит памяти")
    raise PdfExtractionError(result.get("detail") or "Не удалось прочитать PDF")
