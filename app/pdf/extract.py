"""Разбор PDF в нормализованный постраничный текст.

Тендерная документация с ЕИС (zakupki.gov.ru) встречается в трёх видах:

* "нормальный" PDF с текстовым слоем - читается напрямую;
* PDF, собранный конвертером из Word, где часть смыслов (в первую очередь
  таблица штрафов и график этапов) живёт в таблицах - таблицы нужно
  вытаскивать отдельно, иначе строки склеиваются в кашу;
* скан без текстового слоя - текста нет вовсе, и об этом честно надо
  сообщить клиенту, а не отдавать пустую выжимку.

Модуль решает все три случая: основной экстрактор - pdfplumber (текст +
таблицы), резервный - pypdf (быстрее и иногда вытаскивает текст там, где
pdfplumber отдал пустоту), а признак скана выставляется по плотности текста.
"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"

# Ниже этого числа символов на страницу считаем, что текстового слоя нет.
_MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER = 80
# Порог, ниже которого стоит попробовать резервный экстрактор.
_MIN_TOTAL_CHARS_BEFORE_FALLBACK = 200
# Строка считается колонтитулом, если повторяется хотя бы на такой доле страниц.
_HEADER_FOOTER_PAGE_RATIO = 0.6
_HEADER_FOOTER_MIN_PAGES = 4
_HEADER_FOOTER_MAX_LEN = 90
# Сколько верхних и нижних строк страницы рассматриваем как колонтитулы.
_HEADER_FOOTER_WINDOW = 2
# Явная нумерация страниц: удаляется независимо от объёма документа, потому
# что частотный детектор требует минимум четырёх страниц, а короткий проект
# контракта на двух листах тоже нумеруют. Оставленный номер приклеивается к
# последней фразе страницы и попадает в текст требования.
_PAGE_LABEL_RE = re.compile(
    r"^(?:стр\.?|страница|page)\s*\d+(?:\s*(?:из|/|of)\s*\d+)?$", re.IGNORECASE
)
# Одинокое число строкой - номер страницы, но только в шапке или подвале:
# в середине это может быть ячейка или номер пункта.
_BARE_NUMBER_RE = re.compile(r"^[-–—(\[]?\s*\d{1,4}\s*[-–—)\]]?$")


class PdfExtractionError(Exception):
    """Файл не является PDF либо не читается ни одним из бэкендов."""


@dataclass(slots=True)
class Page:
    number: int  # нумерация с единицы - как её видит человек в просмотрщике
    text: str


@dataclass(slots=True)
class ExtractedDocument:
    pages: list[Page] = field(default_factory=list)
    extractor: str = "unknown"

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)

    @property
    def characters(self) -> int:
        return sum(len(page.text) for page in self.pages)

    @property
    def pages_with_text(self) -> int:
        return sum(
            1
            for page in self.pages
            if len(page.text) >= _MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER
        )

    @property
    def likely_scanned(self) -> bool:
        if not self.pages:
            return True
        return self.pages_with_text == 0


def extract_text(data: bytes) -> ExtractedDocument:
    """Вытащить текст из байтов PDF.

    Порядок: pdfplumber → pypdf. Возвращается тот результат, который дал
    больше символов; если оба пусты, документ помечается как скан
    (``likely_scanned``), но исключение не выбрасывается - решение о том,
    что делать с пустым текстом, принимает вызывающий слой.
    """
    if not data:
        raise PdfExtractionError("Загружен пустой файл")
    if not data.lstrip()[:5].startswith(PDF_MAGIC):
        raise PdfExtractionError(
            "Файл не похож на PDF: отсутствует сигнатура %PDF- в начале"
        )

    primary = _extract_with_pdfplumber(data)
    if primary is not None and primary.characters >= _MIN_TOTAL_CHARS_BEFORE_FALLBACK:
        return _postprocess(primary)

    fallback = _extract_with_pypdf(data)
    candidates = [doc for doc in (primary, fallback) if doc is not None]
    if not candidates:
        raise PdfExtractionError(
            "Не удалось прочитать PDF: файл повреждён или защищён паролем"
        )
    best = max(candidates, key=lambda doc: doc.characters)
    return _postprocess(best)


def _extract_with_pdfplumber(data: bytes) -> ExtractedDocument | None:
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - зависимость объявлена в requirements
        logger.warning("pdfplumber недоступен, пропускаем основной экстрактор")
        return None

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages: list[Page] = []
            for index, page in enumerate(pdf.pages, start=1):
                parts: list[str] = []
                raw_text = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                if raw_text:
                    parts.append(raw_text)
                parts.extend(_render_tables(page))
                pages.append(Page(number=index, text="\n".join(parts).strip()))
        return ExtractedDocument(pages=pages, extractor="pdfplumber")
    except Exception as exc:  # noqa: BLE001 - хотим fallback на любой сбой парсера
        logger.warning("pdfplumber не смог разобрать документ: %s", exc)
        return None


def _render_tables(page: object) -> list[str]:
    """Перевести таблицы страницы в текст вида ``a | b | c``.

    Таблицы важны: в них почти всегда лежат штрафы (нарушение → размер) и
    график этапов. Плоский ``extract_text`` склеивает такие ячейки в
    неразбираемую строку, поэтому таблицы добавляются отдельным фрагментом
    с маркером ``[ТАБЛИЦА]``.
    """
    rendered: list[str] = []
    try:
        tables = page.extract_tables()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось извлечь таблицы: %s", exc)
        return rendered

    for table in tables or []:
        rows: list[str] = []
        for row in table:
            cells = [
                re.sub(r"\s+", " ", (cell or "")).strip() for cell in row  # type: ignore[union-attr]
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            rendered.append("[ТАБЛИЦА]\n" + "\n".join(rows))
    return rendered


def _extract_with_pypdf(data: bytes) -> ExtractedDocument | None:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        logger.warning("pypdf недоступен, резервный экстрактор пропущен")
        return None

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Пустой пароль открывает большинство "защищённых от печати" файлов.
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                logger.warning("PDF зашифрован и не открывается пустым паролем: %s", exc)
                return None
        pages = [
            Page(number=index, text=(page.extract_text() or "").strip())
            for index, page in enumerate(reader.pages, start=1)
        ]
        return ExtractedDocument(pages=pages, extractor="pypdf")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypdf не смог разобрать документ: %s", exc)
        return None


def _postprocess(document: ExtractedDocument) -> ExtractedDocument:
    """Нормализация текста и удаление колонтитулов."""
    noise = _detect_header_footer_lines(document)
    for page in document.pages:
        page.text = _normalize(page.text, noise)
    return document


def _detect_header_footer_lines(document: ExtractedDocument) -> set[str]:
    """Найти строки, повторяющиеся в шапке/подвале большинства страниц.

    Колонтитулы вида "Извещение № 0173100007725000123 - стр. 14 из 87"
    занимают заметную долю контекста и мешают чанкингу: они дробят
    осмысленные абзацы. Номера страниц дополнительно маскируются, чтобы
    строки с разными числами схлопывались в один шаблон.
    """
    if len(document.pages) < _HEADER_FOOTER_MIN_PAGES:
        return set()

    counter: Counter[str] = Counter()
    for page in document.pages:
        lines = [line.strip() for line in page.text.splitlines() if line.strip()]
        window = lines[:_HEADER_FOOTER_WINDOW] + lines[-_HEADER_FOOTER_WINDOW:]
        for line in set(window):
            if len(line) <= _HEADER_FOOTER_MAX_LEN:
                counter[_mask_numbers(line)] += 1

    threshold = max(_HEADER_FOOTER_MIN_PAGES, int(len(document.pages) * _HEADER_FOOTER_PAGE_RATIO))
    return {pattern for pattern, count in counter.items() if count >= threshold}


def _mask_numbers(line: str) -> str:
    return re.sub(r"\d+", "#", line)


def _normalize(text: str, noise_patterns: set[str]) -> str:
    if not text:
        return ""

    text = text.replace("\u00a0", " ").replace("\u2011", "-")
    # Слово, разорванное переносом на границе строк, склеиваем обратно.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    filled_indexes = [index for index, line in enumerate(lines) if line]
    edge_indexes = set(
        filled_indexes[:_HEADER_FOOTER_WINDOW] + filled_indexes[-_HEADER_FOOTER_WINDOW:]
    )

    kept: list[str] = []
    for index, stripped in enumerate(lines):
        if not stripped:
            kept.append("")
            continue
        if _mask_numbers(stripped) in noise_patterns:
            continue
        if _PAGE_LABEL_RE.match(stripped):
            continue
        if index in edge_indexes and _BARE_NUMBER_RE.match(stripped):
            continue
        kept.append(stripped)

    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
