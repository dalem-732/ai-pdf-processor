"""Тесты стадии reduce: слияние частичных выжимок и оценка полноты."""

from __future__ import annotations

import pytest

from app.core.merge import merge_summaries, refine_summary, score_confidence
from app.schemas import (
    Deadlines,
    Money,
    Penalty,
    Requirement,
    RequirementCategory,
    Stage,
    TenderSummary,
)


def test_scalars_take_first_non_empty_value() -> None:
    parts = [
        TenderSummary(subject=None, customer=None),
        TenderSummary(subject="Ремонт вентиляции", customer="ГБУЗ № 7"),
        TenderSummary(subject="Другое значение", customer="Другой заказчик"),
    ]

    merged = merge_summaries(parts)

    assert merged.subject == "Ремонт вентиляции"
    assert merged.customer == "ГБУЗ № 7"


def test_price_prefers_entry_with_citation() -> None:
    """Цитата важнее: по ней сумму потом можно сверить с текстом документа."""
    parts = [
        TenderSummary(contract_price=Money(amount=None)),
        TenderSummary(contract_price=Money(amount=12_480_500.0, raw=None)),
        TenderSummary(contract_price=Money(amount=12_480_500.0, raw="12 480 500,00 рублей")),
    ]

    merged = merge_summaries(parts)

    assert merged.contract_price.amount == pytest.approx(12_480_500.0)
    assert merged.contract_price.raw == "12 480 500,00 рублей"


def test_deadlines_merged_field_by_field_with_unique_stages() -> None:
    parts = [
        TenderSummary(deadlines=Deadlines(start="01.03.2026", stages=[Stage(name="Этап 1")])),
        TenderSummary(
            deadlines=Deadlines(
                end="31.08.2026",
                duration="180 календарных дней",
                stages=[Stage(name="Этап 1"), Stage(name="Этап 2", deadline="30.06.2026")],
            )
        ),
    ]

    merged = merge_summaries(parts)

    assert (merged.deadlines.start, merged.deadlines.end) == ("01.03.2026", "31.08.2026")
    assert merged.deadlines.duration == "180 календарных дней"
    assert [stage.name for stage in merged.deadlines.stages] == ["Этап 1", "Этап 2"]


def test_overlapping_requirement_deduplicated_with_pages_merged() -> None:
    """Перекрытие фрагментов даёт один пункт дважды: целиком и усечённым."""
    full = (
        "Наличие действующей лицензии на осуществление деятельности по монтажу "
        "средств обеспечения пожарной безопасности зданий"
    )
    parts = [
        TenderSummary(
            requirements=[
                Requirement(
                    category=RequirementCategory.LICENSE, text=full, source_pages=[3]
                )
            ]
        ),
        TenderSummary(
            requirements=[
                Requirement(
                    category=RequirementCategory.LICENSE,
                    text="Наличие действующей лицензии на осуществление деятельности по монтажу",
                    source_pages=[4],
                )
            ]
        ),
    ]

    merged = merge_summaries(parts)

    assert len(merged.requirements) == 1
    assert merged.requirements[0].text == full
    assert merged.requirements[0].source_pages == [3, 4]


def test_penalty_dedup_keeps_most_informative_record() -> None:
    """Из таблицы и из пересказа остаётся запись с формулой и суммой."""
    violation = "Ненадлежащее качество выполненных работ по контракту"
    parts = [
        TenderSummary(penalties=[Penalty(violation=violation, sanction="Штраф")]),
        TenderSummary(
            penalties=[
                Penalty(
                    violation=violation,
                    sanction="Штраф",
                    formula="10 % цены контракта",
                    amount=Money(amount=1_248_050.0),
                    source_pages=[12],
                )
            ]
        ),
    ]

    merged = merge_summaries(parts)

    assert len(merged.penalties) == 1
    assert merged.penalties[0].formula == "10 % цены контракта"
    assert merged.penalties[0].amount.amount == pytest.approx(1_248_050.0)


def test_distinct_penalties_are_all_kept() -> None:
    parts = [
        TenderSummary(
            penalties=[
                Penalty(violation="Просрочка выполнения этапа работ", sanction="Пеня"),
                Penalty(violation="Привлечение субподрядчика без согласования", sanction="Штраф"),
            ]
        ),
        TenderSummary(penalties=[Penalty(violation="Непредставление документации", sanction="Штраф")]),
    ]

    assert len(merge_summaries(parts).penalties) == 3


def test_empty_input_gives_empty_summary() -> None:
    merged = merge_summaries([])

    assert merged.contract_price is None
    assert merged.requirements == []


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (TenderSummary(), 0.0),
        (TenderSummary(contract_price=Money(amount=100.0)), 0.3),
        (
            TenderSummary(
                contract_price=Money(amount=100.0),
                deadlines=Deadlines(duration="90 дней"),
                requirements=[Requirement(text="Лицензия")],
                penalties=[Penalty(violation="Просрочка", sanction="Пеня")],
            ),
            1.0,
        ),
    ],
)
def test_confidence_reflects_filled_blocks(summary: TenderSummary, expected: float) -> None:
    assert score_confidence(summary) == pytest.approx(expected)


def test_confidence_ignores_price_without_amount() -> None:
    summary = TenderSummary(contract_price=Money(amount=None, raw="цена не определена"))

    assert score_confidence(summary) == 0.0


def test_confidence_penalizes_partial_document_coverage() -> None:
    summary = TenderSummary(
        contract_price=Money(amount=100.0),
        deadlines=Deadlines(duration="90 дней"),
        requirements=[Requirement(text="Лицензия")],
        penalties=[Penalty(violation="Просрочка", sanction="Пеня")],
    )

    full = score_confidence(summary, chunks_sent=25, chunks_total=25)
    partial = score_confidence(summary, chunks_sent=12, chunks_total=25)

    assert full == pytest.approx(1.0)
    assert partial < full


def test_refine_moves_administrative_deadlines_to_raw_mentions() -> None:
    summary = TenderSummary(
        deadlines=Deadlines(
            stages=[
                Stage(name="первый этап: проектная документация", deadline=None),
                Stage(name="Первый этап", deadline=None),
                Stage(name="Возврат обеспечения исполнения договора", deadline="60 календарных дней"),
                Stage(name="Выполнение работ по капитальному ремонту", deadline="90 календарных дней"),
            ]
        )
    )

    refined = refine_summary(summary)

    stage_names = [stage.name for stage in refined.deadlines.stages]
    assert len(stage_names) == 2
    assert any("перв" in name.lower() for name in stage_names)
    assert any("капитальн" in name.lower() for name in stage_names)
    assert any("Возврат обеспечения" in mention for mention in refined.deadlines.raw_mentions)


def test_refine_keeps_only_monetary_penalties() -> None:
    summary = TenderSummary(
        penalties=[
            Penalty(
                violation="Просрочка выполнения работ",
                sanction="Пеня",
                formula="1/130 ставки рефинансирования",
            ),
            Penalty(
                violation="Задержка начала работ более чем на 10 дней",
                sanction="одностороннее расторжение договора",
            ),
            Penalty(
                violation="Несвоевременная оплата выполненных работ",
                sanction="неустойка",
                formula="1/130",
            ),
            Penalty(
                violation="Неисполнение обязательств при однократном нарушении",
                sanction="штраф",
                amount=Money(amount=10_000.0),
            ),
        ]
    )

    refined = refine_summary(summary)

    assert len(refined.penalties) == 2
    assert all("расторжен" not in item.sanction.lower() for item in refined.penalties)
    assert all("оплат" not in item.violation.lower() for item in refined.penalties)


def test_refine_prioritizes_qualification_requirements() -> None:
    summary = TenderSummary(
        requirements=[
            Requirement(category=RequirementCategory.OTHER, text="Обеспечить вывоз строительного мусора"),
            Requirement(
                category=RequirementCategory.LICENSE,
                text="Действующее членство в СРО на проектирование",
            ),
            Requirement(category=RequirementCategory.OTHER, text="Вести видеофиксацию скрытых работ"),
            Requirement(
                category=RequirementCategory.FINANCIAL,
                text="Независимая гарантия в размере 10 % от НМЦК",
            ),
        ]
    )

    refined = refine_summary(summary)

    assert refined.requirements[0].category is RequirementCategory.LICENSE
    assert refined.requirements[1].category is RequirementCategory.FINANCIAL
    assert all("видеофиксац" not in item.text.lower() for item in refined.requirements[:2])
