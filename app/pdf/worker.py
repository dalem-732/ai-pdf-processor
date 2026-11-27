"""Дочерний процесс разбора PDF: байты на stdin, JSON с текстом на stdout."""

from __future__ import annotations

import importlib
import json
import sys

from app.pdf.extract import PdfExtractionError, PdfPageLimitError, extract_text

try:
    import resource
except ImportError:
    resource = None


def _apply_memory_limit(memory_mb: int) -> None:
    if not memory_mb or resource is None:
        return
    limit = memory_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        return


def _run(data: bytes, max_pages: int) -> dict[str, object]:
    try:
        document = extract_text(data, max_pages=max_pages)
    except PdfPageLimitError as exc:
        return {"status": "pages", "detail": str(exc)}
    except PdfExtractionError as exc:
        return {"status": "error", "detail": str(exc)}
    except MemoryError:
        return {"status": "memory", "detail": ""}
    return {
        "status": "ok",
        "extractor": document.extractor,
        "pages": [{"number": page.number, "text": page.text} for page in document.pages],
    }


def main() -> int:
    max_pages = int(sys.argv[1])
    memory_mb = int(sys.argv[2])
    data = sys.stdin.buffer.read()
    for module in ("pdfplumber", "pypdf"):
        importlib.import_module(module)
    _apply_memory_limit(memory_mb)
    try:
        result = _run(data, max_pages)
    except Exception:
        result = {"status": "error", "detail": "Не удалось прочитать PDF"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
