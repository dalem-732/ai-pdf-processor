"""Тесты нарезки документа и отбора фрагментов."""

from __future__ import annotations

from app.core.chunking import build_chunks, build_marked_text, select_chunks
from app.pdf.extract import ExtractedDocument, Page


def _document(pages: int, page_chars: int = 500) -> ExtractedDocument:
    return ExtractedDocument(
        extractor="test",
        pages=[
            Page(number=index, text=f"Страница {index}. " + "текст " * (page_chars // 6))
            for index in range(1, pages + 1)
        ],
    )


def test_marked_text_contains_page_markers(tender_document: ExtractedDocument) -> None:
    marked = build_marked_text(tender_document)

    assert "[[стр. 1]]" in marked
    assert "[[стр. 2]]" in marked


def test_short_document_fits_single_chunk(tender_document: ExtractedDocument) -> None:
    chunks = build_chunks(tender_document, chunk_chars=12_000, overlap_chars=500)

    assert len(chunks) == 1
    assert chunks[0].first_page == 1
    assert chunks[0].last_page == 2


def test_long_document_split_into_chunks_with_page_ranges() -> None:
    chunks = build_chunks(_document(pages=10, page_chars=600), chunk_chars=1_500, overlap_chars=200)

    assert len(chunks) > 1
    assert all(chunk.text for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    # Диапазоны страниц идут по возрастанию и покрывают документ.
    assert chunks[0].first_page == 1
    assert chunks[-1].last_page == 10


def test_chunks_stay_within_size_budget() -> None:
    limit = 2_000
    chunks = build_chunks(_document(pages=12, page_chars=700), chunk_chars=limit, overlap_chars=150)

    # Допуск на маркеры страниц и перекрытие, но без кратного превышения:
    # именно из-за него запрос упирался бы в контекст модели.
    assert all(len(chunk.text) <= limit * 1.5 for chunk in chunks)


def test_oversized_single_page_is_split() -> None:
    document = ExtractedDocument(
        extractor="test",
        pages=[Page(number=1, text="абзац. " * 2_000)],
    )

    chunks = build_chunks(document, chunk_chars=1_000, overlap_chars=0)

    assert len(chunks) > 1


def test_selection_limits_count_keeps_order_and_first_chunk() -> None:
    chunks = build_chunks(_document(pages=20, page_chars=600), chunk_chars=1_200, overlap_chars=100)
    assert len(chunks) > 4

    selected = select_chunks(chunks, max_chunks=4)

    assert len(selected) == 4
    assert selected[0].index == 0, "титульный фрагмент включается всегда"
    assert [chunk.index for chunk in selected] == sorted(chunk.index for chunk in selected)


def test_selection_prefers_relevant_chunks() -> None:
    """Фрагмент с ценой и штрафами должен вытеснить приложения с формами."""
    pages = [Page(number=1, text="Титульный лист документации. " * 20)]
    pages += [
        Page(number=index, text="Приложение с формой описи документов. " * 30)
        for index in range(2, 8)
    ]
    pages.append(
        Page(
            number=8,
            text=(
                "Начальная (максимальная) цена контракта: 5 000 000,00 рублей. "
                "Срок выполнения 120 календарных дней. Ответственность сторон: "
                "штраф и пени за просрочку. Требования к участнику закупки. " * 5
            ),
        )
    )
    chunks = build_chunks(
        ExtractedDocument(pages=pages, extractor="test"), chunk_chars=1_100, overlap_chars=100
    )

    selected = select_chunks(chunks, max_chunks=2)

    assert any("Начальная (максимальная) цена" in chunk.text for chunk in selected)
