"""Тесты детерминированного извлечения фактов."""

from __future__ import annotations

import pytest

from app.core import heuristics
from app.core.heuristics import (
    APPLICATION_SECURITY_KEYWORDS,
    CONTRACT_SECURITY_KEYWORDS,
    PRICE_KEYWORDS,
    extract_deadlines,
    extract_penalties,
    extract_requirements,
    find_money,
    heuristic_summary,
    money_near_keywords,
    page_at,
    reflow,
    relevance_score,
)
from app.schemas import RequirementCategory


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("сумма 1 000 руб.", 1000.0),
        ("сумма 1 234 567,89 рублей", 1234567.89),
        ("сумма 12 480 500,00 (двенадцать миллионов) рублей 00 копеек", 12480500.0),
        ("сумма 500 тыс. руб.", 500_000.0),
        ("сумма 1,5 млн руб.", 1_500_000.0),
        ("цена 99 ₽", 99.0),
    ],
)
def test_find_money_parses_procurement_formats(text: str, expected: float) -> None:
    """Форматы сумм, реально встречающиеся в документации с ЕИС."""
    found = find_money(text)

    assert found, f"сумма не найдена в {text!r}"
    assert found[0][0].amount == pytest.approx(expected)


def test_money_vat_flag_from_context() -> None:
    included = find_money("Цена 100 000,00 рублей, включая НДС 20 %")[0][0]
    excluded = find_money("Цена 100 000,00 рублей без НДС")[0][0]

    assert included.includes_vat is True
    assert excluded.includes_vat is False


def test_price_bound_to_nearest_keyword_not_to_first_number() -> None:
    """Сумма привязывается к своей ключевой фразе, а не к первой в тексте."""
    text = (
        "Размер обеспечения заявки: 124 805,00 рублей. "
        "Начальная (максимальная) цена контракта составляет 12 480 500,00 рублей. "
        "Размер обеспечения исполнения контракта: 624 025,00 рублей."
    )

    assert money_near_keywords(text, PRICE_KEYWORDS).amount == pytest.approx(12_480_500.0)
    assert money_near_keywords(text, APPLICATION_SECURITY_KEYWORDS).amount == pytest.approx(124_805.0)
    assert money_near_keywords(text, CONTRACT_SECURITY_KEYWORDS).amount == pytest.approx(624_025.0)


def test_money_not_found_returns_none() -> None:
    assert money_near_keywords("Документация не содержит цифр", PRICE_KEYWORDS) is None


def test_deadlines_extracted_with_stages() -> None:
    text = (
        "Срок начала выполнения работ: с даты заключения контракта, но не ранее 01.03.2026.\n"
        "Срок окончания выполнения работ: 31.08.2026.\n"
        "Общий срок выполнения работ составляет 180 календарных дней.\n"
        "Этап 1 - демонтаж: в течение 30 календарных дней.\n"
        "Этап 2 - монтаж: до 30.06.2026.\n"
    )

    deadlines = extract_deadlines(text)

    assert deadlines.start == "01.03.2026"
    assert deadlines.end == "31.08.2026"
    assert deadlines.duration == "180 календарных дней"
    assert [stage.deadline for stage in deadlines.stages] == [
        "в течение 30 календарных дней",
        "30.06.2026",
    ]


def test_textual_date_recognized() -> None:
    deadlines = extract_deadlines('Срок окончания выполнения работ: «15» декабря 2026 г.')

    assert deadlines.end == '«15» декабря 2026 г.'


def test_requirements_scoped_to_section_and_categorized() -> None:
    text = (
        "Требования к участнику закупки:\n"
        "1.1. Наличие действующей лицензии на монтаж средств пожарной безопасности.\n"
        "1.2. Опыт исполнения не менее двух аналогичных контрактов за три года.\n"
        "1.3. Наличие в штате не менее пяти квалифицированных специалистов.\n"
        "Ответственность сторон\n"
        "2.1. Подрядчик должен уплатить штраф в размере 5 000,00 рублей за нарушение.\n"
    )

    requirements = extract_requirements(text)
    categories = [item.category for item in requirements]

    assert RequirementCategory.LICENSE in categories
    assert RequirementCategory.EXPERIENCE in categories
    assert RequirementCategory.STAFF in categories
    # Пункт об ответственности лежит за границей раздела требований и не должен
    # попасть в требования, хотя содержит слово «должен».
    assert not any("штраф" in item.text.lower() for item in requirements)


def test_requirement_keyword_matching_respects_word_boundaries() -> None:
    """ "сро" внутри "просрочки" не должно превращаться в требование о СРО."""
    penalty_clause = "За каждый день просрочки поставки начисляется пеня 0,1 % цены."

    categories = [item.category for item in extract_requirements(penalty_clause)]

    assert RequirementCategory.QUALIFICATION not in categories


def test_contract_price_from_dogovor_wording() -> None:
    text = (
        "3.1. Цена Договора, определенная по результатам аукциона, "
        "составляет 13 515 425,00 (тринадцать миллионов) руб., в том числе НДС 5%."
    )

    price = money_near_keywords(text, PRICE_KEYWORDS)

    assert price is not None
    assert price.amount == pytest.approx(13_515_425.0)


def test_contract_customer_skips_role_placeholder() -> None:
    text = (
        "Некоммерческая организация «Фонд капитального ремонта общего имущества "
        "в многоквартирных домах в Республике Бурятия», в лице директора "
        "(далее – Заказчик), с одной стороны, и общество с ограниченной "
        "ответственностью «ФИНАНС-СТРОЙ» (далее – Подрядчик), с другой стороны."
    )

    customer = heuristics.extract_customer(text)

    assert customer is not None
    assert "Фонд капитального ремонта" in customer
    assert "ФИНАНС-СТРОЙ" not in customer


def test_contract_subject_from_predmet_dogovora() -> None:
    text = (
        "1. Предмет Договора\n"
        "1.1. Подрядчик обязуется разработать проектную документацию на капитальный ремонт. "
        "1.2. Выполнить работы по капитальному ремонту крыши."
    )

    subject = heuristics.extract_subject(text)

    assert subject is not None
    assert "проектную документацию" in subject.lower()


def test_penalty_text_cleans_page_marker_artifacts() -> None:
    text = "33]] обязан уплатить Заказчику штраф в размере 10 000,00 рублей."

    penalties = extract_penalties(text)

    assert penalties
    assert "33]]" not in penalties[0].violation


def test_penalties_from_table_rows() -> None:
    text = (
        "[ТАБЛИЦА]\n"
        "Нарушение | Мера ответственности | Размер\n"
        "Просрочка выполнения работ | Пеня за каждый день | 1/300 ключевой ставки\n"
        "Ненадлежащее качество | Штраф | 10 000,00 руб.\n"
    )

    penalties = extract_penalties(text)
    violations = [item.violation for item in penalties]

    assert "Просрочка выполнения работ" in violations
    assert "Ненадлежащее качество" in violations
    # Шапка таблицы - не нарушение.
    assert "Нарушение" not in violations

    quality = next(item for item in penalties if item.violation == "Ненадлежащее качество")
    assert quality.amount is not None
    assert quality.amount.amount == pytest.approx(10_000.0)


def test_penalties_skip_limiting_clauses() -> None:
    """ "Сумма штрафов не может превышать цену контракта" - не штраф."""
    text = "Общая сумма начисленных штрафов не может превышать цену контракта."

    assert extract_penalties(text) == []


def test_reflow_joins_wrapped_lines_but_keeps_structure() -> None:
    text = (
        "Опыт исполнения не менее двух контрактов за\n"
        "последние три года.\n"
        "1.2. Следующий пункт документации.\n"
        "Нарушение | Штраф | 1 000,00 руб.\n"
    )

    lines = reflow(text).splitlines()

    assert lines[0] == "Опыт исполнения не менее двух контрактов за последние три года."
    assert lines[1] == "1.2. Следующий пункт документации."
    assert lines[2] == "Нарушение | Штраф | 1 000,00 руб."


def test_reflow_does_not_glue_number_continuation_as_list_item() -> None:
    """Строка "31 Федерального закона..." - продолжение фразы, а не пункт."""
    text = "Участник представляет декларацию по статье\n31 Федерального закона № 44-ФЗ.\n"

    assert reflow(text).count("\n") == 0


def test_page_markers_map_positions_to_pages() -> None:
    text = (
        f"{heuristics.PAGE_MARKER_TEMPLATE.format(page=4)}\nтекст страницы четыре\n"
        f"{heuristics.PAGE_MARKER_TEMPLATE.format(page=5)}\nтекст страницы пять"
    )

    assert page_at(text, text.index("четыре")) == 4
    assert page_at(text, text.index("пять")) == 5
    assert page_at("без маркеров", 3) is None


def test_relevance_score_prefers_meaningful_chunk() -> None:
    meaningful = (
        "Начальная (максимальная) цена контракта: 1 000 000,00 рублей. "
        "Срок выполнения - 90 календарных дней. Ответственность сторон: пени и штрафы."
    )
    noise = "Приложение № 4. Форма описи документов, представляемых участником."

    assert relevance_score(meaningful) > relevance_score(noise)


def test_heuristic_summary_covers_all_requested_blocks(sample_pdf_bytes: bytes) -> None:
    """Итоговая проверка офлайн-режима на реальном PDF: все четыре раздела задания."""
    from app.core.chunking import build_marked_text
    from app.pdf.extract import extract_text

    summary = heuristic_summary(build_marked_text(extract_text(sample_pdf_bytes)))

    assert summary.contract_price is not None
    assert summary.contract_price.amount == pytest.approx(12_480_500.0)
    assert summary.deadlines.duration == "180 календарных дней"
    assert len(summary.requirements) >= 5
    assert len(summary.penalties) >= 5
    assert any(item.source_pages for item in summary.penalties)
