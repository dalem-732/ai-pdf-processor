"""Тесты сверки выжимки с исходным текстом.

Здесь проверяется главная защита сервиса: галлюцинация в сумме контракта не
должна уехать пользователю молча.
"""

from __future__ import annotations

import pytest

from app.core.validation import verify_and_enrich
from app.schemas import Deadlines, Money, Penalty, Requirement, TenderSummary

DOCUMENT = (
    "[[стр. 1]]\n"
    'Заказчик: ГБУЗ «Городская больница № 3».\n'
    "Предмет закупки: поставка медицинского оборудования.\n"
    "Начальная (максимальная) цена контракта составляет 3 250 000,50 рублей, "
    "включая НДС 20 %.\n"
    "Размер обеспечения исполнения контракта: 162 500,00 рублей.\n"
    "Срок окончания выполнения работ: 30.09.2026.\n"
    "Общий срок выполнения работ составляет 90 календарных дней.\n"
    "[[стр. 2]]\n"
    "Требования к участнику закупки:\n"
    "1.1. Наличие лицензии на обслуживание медицинской техники.\n"
    "Ответственность сторон\n"
    "2.1. За просрочку поставки начисляется пеня в размере 0,1 % цены контракта.\n"
)


def test_missing_price_is_filled_from_rules_with_warning() -> None:
    summary = verify_and_enrich(TenderSummary(), DOCUMENT)

    assert summary.contract_price is not None
    assert summary.contract_price.amount == pytest.approx(3_250_000.50)
    assert any("подставлено найденное правилами" in warning for warning in summary.warnings)


def test_hallucinated_amount_is_flagged() -> None:
    """Сумма, которой нет в тексте, помечается предупреждением."""
    summary = verify_and_enrich(
        TenderSummary(contract_price=Money(amount=9_999_999.0, raw="9 999 999,00 рублей")),
        DOCUMENT,
    )

    assert any("не найдено в тексте" in warning for warning in summary.warnings)
    assert any("правила нашли в тексте другое значение" in warning.lower() for warning in summary.warnings)


def test_correct_amount_passes_without_warnings() -> None:
    summary = verify_and_enrich(
        TenderSummary(
            contract_price=Money(amount=3_250_000.50, raw="3 250 000,50 рублей"),
            deadlines=Deadlines(duration="90 календарных дней"),
            requirements=[Requirement(text="Наличие лицензии")],
            penalties=[Penalty(violation="Просрочка поставки", sanction="Пеня")],
        ),
        DOCUMENT,
    )

    assert summary.warnings == []
    assert summary.contract_price.amount == pytest.approx(3_250_000.50)


def test_amount_found_despite_non_breaking_spaces() -> None:
    """Разряды в PDF разделены неразрывными пробелами, сумма всё равно сверяется."""
    document = "Цена контракта составляет 1\u00a0250\u00a0000,00 рублей."

    summary = verify_and_enrich(TenderSummary(contract_price=Money(amount=1_250_000.0)), document)

    assert not any("не найдено в тексте" in warning for warning in summary.warnings)


def test_vat_flag_and_citation_backfilled_from_rules() -> None:
    summary = verify_and_enrich(
        TenderSummary(contract_price=Money(amount=3_250_000.50)), DOCUMENT
    )

    assert summary.contract_price.includes_vat is True
    assert summary.contract_price.raw


def test_empty_blocks_are_recovered_from_rules() -> None:
    summary = verify_and_enrich(TenderSummary(), DOCUMENT)

    assert summary.requirements, "требования должны быть восстановлены правилами"
    assert summary.penalties, "штрафы должны быть восстановлены правилами"
    assert summary.deadlines.duration == "90 календарных дней"
    assert any("восстановлены правилами" in warning for warning in summary.warnings)


def test_document_without_facts_reports_honest_warnings() -> None:
    summary = verify_and_enrich(TenderSummary(), "[[стр. 1]]\nОбщие положения без цифр.")

    joined = " ".join(summary.warnings)
    assert "Сумма контракта не найдена" in joined
    assert "Сроки выполнения" in joined
    assert "Требования к исполнителю в документе не найдены" in joined
    assert "Штрафы и пени в документе не найдены" in joined


def test_warnings_are_deduplicated() -> None:
    summary = TenderSummary(warnings=["дубль", "дубль"])

    assert verify_and_enrich(summary, DOCUMENT).warnings.count("дубль") == 1


def test_procurement_number_is_normalized_to_eis_digits() -> None:
    summary = verify_and_enrich(
        TenderSummary(
            procurement_number=(
                "Электронный аукцион № 200200000022600039 (протокол от 09.02.2026 № 54/2026)"
            )
        ),
        DOCUMENT,
    )

    assert summary.procurement_number == "200200000022600039"
