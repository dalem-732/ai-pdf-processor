"""Тесты извлечения текста из PDF."""

from __future__ import annotations

import pytest

from app.pdf.extract import (
    ExtractedDocument,
    Page,
    PdfExtractionError,
    _detect_header_footer_lines,
    _normalize,
    extract_text,
)


def test_extract_real_pdf_returns_pages_and_text(sample_pdf_bytes: bytes) -> None:
    document = extract_text(sample_pdf_bytes)

    assert document.extractor in {"pdfplumber", "pypdf"}
    assert document.pages, "PDF должен разобраться хотя бы на одну страницу"
    assert not document.likely_scanned
    assert "цена контракта" in document.text.lower()


def test_extract_keeps_table_cells_separated(sample_pdf_bytes: bytes) -> None:
    """Таблица штрафов должна попасть в текст строками с разделителями.

    Именно это отличает пригодный для анализа текст от каши
    """
    document = extract_text(sample_pdf_bytes)

    assert "[ТАБЛИЦА]" in document.text
    assert "Ненадлежащее качество работ | Штраф |" in document.text


def test_extract_rejects_non_pdf() -> None:
    with pytest.raises(PdfExtractionError, match="сигнатура"):
        extract_text(b"\x89PNG\r\n\x1a\n not a pdf at all")


def test_extract_rejects_empty_payload() -> None:
    with pytest.raises(PdfExtractionError, match="пустой"):
        extract_text(b"")


def test_extract_reports_unreadable_pdf() -> None:
    with pytest.raises(PdfExtractionError, match="повреждён|прочитать"):
        extract_text("%PDF-1.7\nвсё, дальше мусор".encode())


def test_normalize_glues_hyphenated_line_break() -> None:
    assert _normalize("подряд-\nчик обязан", set()) == "подрядчик обязан"


def test_normalize_drops_detected_noise_lines() -> None:
    assert _normalize("Страница 7 из 88\nтекст пункта", {"Страница # из #"}) == "текст пункта"


def test_page_label_removed_even_in_short_document() -> None:
    """ "Страница 1 из 4" удаляется без частотного анализа.

    Частотный детектор требует минимум четырёх страниц, а короткий проект
    контракта тоже нумеруют. Оставленный номер приклеивается к последней фразе
    страницы и уезжает в текст требования.
    """
    text = "Опыт исполнения не менее двух контрактов\nСтраница 1 из 4"

    assert _normalize(text, set()) == "Опыт исполнения не менее двух контрактов"


def test_bare_number_dropped_at_page_edge_but_kept_inside() -> None:
    at_edge = _normalize("Текст пункта документации\n17", set())
    inside = _normalize("Строка 1\nСтрока 2\n17\nСтрока 3\nСтрока 4", set())

    assert at_edge == "Текст пункта документации"
    assert "17" in inside.splitlines()


def test_header_footer_detected_across_pages() -> None:
    pages = [
        Page(number=index, text=f"Извещение № 0173100 стр. {index}\nСодержательный текст пункта")
        for index in range(1, 7)
    ]
    noise = _detect_header_footer_lines(ExtractedDocument(pages=pages, extractor="test"))

    assert "Извещение № # стр. #" in noise


def test_header_footer_not_detected_in_short_document() -> None:
    """На двух страницах повтор строки - ещё не колонтитул.

    Порог по числу страниц защищает от удаления содержательного текста из
    коротких документов вроде проекта контракта на трёх листах.
    """
    pages = [Page(number=index, text="Повтор\nтекст") for index in (1, 2)]

    assert _detect_header_footer_lines(ExtractedDocument(pages=pages)) == set()


def test_scanned_document_is_flagged() -> None:
    document = ExtractedDocument(pages=[Page(number=1, text=""), Page(number=2, text="   ")])

    assert document.likely_scanned
    assert document.pages_with_text == 0
