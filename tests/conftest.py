"""Общие фикстуры тестов."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.pdf.extract import ExtractedDocument, Page
from scripts.make_sample_pdf import build_sample_pdf


@pytest.fixture(scope="session")
def sample_pdf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Синтетическая тендерная документация в PDF, собирается один раз на сессию."""
    target = tmp_path_factory.mktemp("pdf") / "sample_tender.pdf"
    return build_sample_pdf(target)


@pytest.fixture(scope="session")
def sample_pdf_bytes(sample_pdf_path: Path) -> bytes:
    return sample_pdf_path.read_bytes()


@pytest.fixture
def stub_settings() -> Settings:
    """Настройки офлайн-режима: без сети, ключей и внешних вызовов."""
    return Settings(llm_provider="stub", max_chunks=8, chunk_chars=12_000)


@pytest.fixture
def tender_document() -> ExtractedDocument:
    """Минимальный документ из двух страниц с типовыми формулировками 44-ФЗ."""
    return ExtractedDocument(
        extractor="test",
        pages=[
            Page(
                number=1,
                text=(
                    'Заказчик: ГБУЗ «Городская больница № 3».\n'
                    "Предмет закупки: поставка медицинского оборудования.\n"
                    "Начальная (максимальная) цена контракта составляет "
                    "3 250 000,50 рублей, включая НДС 20 %.\n"
                    "Размер обеспечения исполнения контракта: 162 500,00 рублей.\n"
                    "Срок начала выполнения работ: 01.04.2026.\n"
                    "Срок окончания выполнения работ: 30.09.2026.\n"
                    "Общий срок выполнения работ составляет 90 календарных дней."
                ),
            ),
            Page(
                number=2,
                text=(
                    "Требования к участнику закупки:\n"
                    "1.1. Наличие действующей лицензии на техническое обслуживание "
                    "медицинской техники.\n"
                    "1.2. Опыт исполнения не менее трёх аналогичных контрактов за "
                    "последние два года.\n"
                    "Ответственность сторон\n"
                    "2.1. За просрочку поставки начисляется пеня в размере 0,1 % "
                    "цены контракта за каждый день просрочки.\n"
                    "[ТАБЛИЦА]\n"
                    "Нарушение | Мера ответственности | Размер\n"
                    "Непредставление документов | Штраф | 10 000,00 руб.\n"
                ),
            ),
        ],
    )
