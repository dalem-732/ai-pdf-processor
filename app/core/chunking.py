"""Нарезка документа на части и отбор самых информативных из них.

Документация с ЕИС легко достигает сотни страниц, а нужные факты занимают в
ней несколько абзацев. Слать всё целиком нельзя: упрёмся в контекст модели
(особенно локальной, где 8k токенов - уже щедро) и заплатим за килобайты
воды. Поэтому текст режется на перекрывающиеся части, а затем отбирается
подмножество, где по ключевым словам вероятнее всего лежат цена, сроки,
требования и штрафы.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.heuristics import PAGE_MARKER_TEMPLATE, relevance_score
from app.pdf.extract import ExtractedDocument

_PARAGRAPH_BOUNDARY_RE = re.compile(r"\n\s*\n")


@dataclass(slots=True)
class Chunk:
    index: int
    text: str
    first_page: int
    last_page: int
    score: float = 0.0

    @property
    def label(self) -> str:
        if self.first_page == self.last_page:
            return f"стр. {self.first_page}"
        return f"стр. {self.first_page}–{self.last_page}"


def build_marked_text(document: ExtractedDocument) -> str:
    """Склеить страницы, разметив их маркерами ``[[стр. N]]``.

    Маркеры служат двум целям: LLM по ним заполняет source_pages, а
    эвристики через них восстанавливают страницу по смещению в тексте. Так
    любой факт в выжимке остаётся прослеживаемым до страницы исходника.
    """
    parts: list[str] = []
    for page in document.pages:
        if not page.text:
            continue
        parts.append(f"{PAGE_MARKER_TEMPLATE.format(page=page.number)}\n{page.text}")
    return "\n\n".join(parts)


def build_chunks(
    document: ExtractedDocument, *, chunk_chars: int, overlap_chars: int
) -> list[Chunk]:
    """Нарезать документ на части по границам страниц и абзацев.

    При нарезке предпочитаются границы страниц и абзацев: разрыв посреди пункта
    "5.2. За неисполнение ... подрядчик уплачивает штраф в размере" оставляет
    модели нарушение без санкции. Перекрытие ``overlap_chars`` страхует те
    разрывы, которых избежать не удалось (в том числе при делении длинной
    страницы по символам).
    """
    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_len = 0
    first_page: int | None = None
    last_page: int | None = None

    def flush() -> None:
        nonlocal buffer, buffer_len, first_page, last_page
        if not buffer or first_page is None or last_page is None:
            return
        text = "\n\n".join(buffer).strip()
        if text:
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=text,
                    first_page=first_page,
                    last_page=last_page,
                    score=relevance_score(text),
                )
            )
        tail = _tail(text, overlap_chars) if overlap_chars else ""
        buffer = [tail] if tail else []
        buffer_len = len(tail)
        first_page = last_page if tail else None

    for page in document.pages:
        if not page.text:
            continue
        for block in _split_page(page.text, chunk_chars):
            piece = f"{PAGE_MARKER_TEMPLATE.format(page=page.number)}\n{block}"
            if buffer and buffer_len + len(piece) > chunk_chars:
                flush()
            if first_page is None:
                first_page = page.number
            last_page = page.number
            buffer.append(piece)
            buffer_len += len(piece) + 2

    flush()
    # Последний flush оставляет в буфере "хвост" перекрытия - самостоятельным
    # чанком он не является, поэтому просто выходим.
    return chunks


def select_chunks(chunks: list[Chunk], *, max_chunks: int) -> list[Chunk]:
    """Отобрать не более ``max_chunks`` частей, сохранив порядок документа.

    Первая часть берётся всегда: там титульный лист с заказчиком, предметом
    закупки и номером извещения - без ключевых слов о штрафах и сроках она
    проиграла бы скорингу, а её содержимое нужно почти всегда. Остальные
    места распределяются по релевантности.
    """
    if len(chunks) <= max_chunks:
        return chunks

    mandatory = chunks[:1]
    rest = sorted(chunks[1:], key=lambda chunk: chunk.score, reverse=True)
    selected = mandatory + rest[: max_chunks - 1]
    return sorted(selected, key=lambda chunk: chunk.index)


def _split_page(text: str, limit: int) -> list[str]:
    """Разбить слишком длинную страницу по абзацам, в крайнем случае - жёстко."""
    if len(text) <= limit:
        return [text]

    blocks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in _PARAGRAPH_BOUNDARY_RE.split(text):
        if current and current_len + len(paragraph) > limit:
            blocks.append("\n\n".join(current))
            current, current_len = [], 0
        if len(paragraph) > limit:
            # Абзац-монстр (обычно склеенная таблица) режем по символам:
            # потерять часть таблицы лучше, чем не отправить страницу вовсе.
            for start in range(0, len(paragraph), limit):
                blocks.append(paragraph[start : start + limit])
            continue
        current.append(paragraph)
        current_len += len(paragraph) + 2
    if current:
        blocks.append("\n\n".join(current))
    return [block for block in blocks if block.strip()]


def _tail(text: str, size: int) -> str:
    """Хвост текста для перекрытия, обрезанный по границе абзаца или строки."""
    if size <= 0 or len(text) <= size:
        return ""
    tail = text[-size:]
    for separator in ("\n\n", "\n", ". "):
        position = tail.find(separator)
        if position != -1:
            return tail[position + len(separator) :].strip()
    return tail.strip()
