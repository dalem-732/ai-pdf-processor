"""Сверка ответа модели с исходным текстом и дополнение пропусков правилами.

Ключевая идея: LLM хорошо понимает формулировки, но может ошибиться в цифре,
а цифра здесь - деньги. Поэтому суммы контракта и обеспечений проверяются по
исходному тексту, а пустые поля восстанавливаются регулярными правилами. В
``warnings`` попадают критичные расхождения и случаи, где модель ничего не
извлекла; часть полей может быть дополнена правилами без отдельного
предупреждения.
"""

from __future__ import annotations

import re

from app.core import heuristics
from app.schemas import Money, TenderSummary

# Расхождение более чем на рубль считаем разными суммами: копеечные различия
# возникают из-за округления при разборе прописи.
_AMOUNT_TOLERANCE = 1.0
_DIGIT_GROUP_SEPARATOR_RE = re.compile(r"(?<=\d)[\s\u00a0](?=\d)")
_EIS_NUMBER_RE = re.compile(r"\b(\d{18,20})\b")


def verify_and_enrich(summary: TenderSummary, marked_text: str) -> TenderSummary:
    """Проверить выжимку по тексту и дополнить её найденным правилами."""
    reference = heuristics.heuristic_summary(marked_text)
    digits = _digits_only_numbers(heuristics.strip_page_markers(marked_text))

    _check_money_field(
        summary, reference, digits, "contract_price", "Сумма контракта", critical=True
    )
    _check_money_field(
        summary,
        reference,
        digits,
        "contract_security",
        "Обеспечение исполнения контракта",
        critical=False,
    )
    _check_money_field(
        summary, reference, digits, "application_security", "Обеспечение заявки", critical=False
    )

    _fill_scalar(summary, reference, "subject")
    _fill_scalar(summary, reference, "customer")
    _fill_scalar(summary, reference, "procurement_number")
    summary.procurement_number = _normalize_procurement_number(summary.procurement_number)

    for field in ("start", "end", "duration"):
        if not getattr(summary.deadlines, field) and getattr(reference.deadlines, field):
            setattr(summary.deadlines, field, getattr(reference.deadlines, field))
    if not summary.deadlines.stages and reference.deadlines.stages:
        summary.deadlines.stages = reference.deadlines.stages
    if not summary.deadlines.raw_mentions and reference.deadlines.raw_mentions:
        summary.deadlines.raw_mentions = reference.deadlines.raw_mentions
    if not any(
        (
            summary.deadlines.start,
            summary.deadlines.end,
            summary.deadlines.duration,
            summary.deadlines.stages,
        )
    ):
        summary.warnings.append(
            "Сроки выполнения в документе не найдены - проверьте проект контракта "
            "или отдельное приложение с графиком работ"
        )

    if not summary.requirements:
        if reference.requirements:
            summary.requirements = reference.requirements
            summary.warnings.append(
                "Требования к исполнителю восстановлены правилами: модель не "
                "выделила их во фрагментах документа"
            )
        else:
            summary.warnings.append("Требования к исполнителю в документе не найдены")

    if not summary.penalties:
        if reference.penalties:
            summary.penalties = reference.penalties
            summary.warnings.append(
                "Штрафы и пени восстановлены правилами: модель не выделила их во "
                "фрагментах документа"
            )
        else:
            summary.warnings.append(
                "Штрафы и пени в документе не найдены - раздел об ответственности "
                "может быть в отдельном файле проекта контракта"
            )

    summary.warnings = list(dict.fromkeys(summary.warnings))
    return summary


def _check_money_field(
    summary: TenderSummary,
    reference: TenderSummary,
    digits: str,
    field: str,
    label: str,
    *,
    critical: bool,
) -> None:
    """Сверить одно денежное поле выжимки с текстом документа.

    ``critical`` отделяет цену контракта от второстепенных сумм: подстановка
    обеспечения правилами - обычное дополнение и в предупреждениях не нуждается,
    а вот то, что цену контракта модель не нашла, пользователь знать обязан.
    """
    value: Money | None = getattr(summary, field)
    fallback: Money | None = getattr(reference, field)

    if value is None or value.amount is None:
        if fallback is not None:
            setattr(summary, field, fallback)
            if critical:
                summary.warnings.append(
                    f"{label}: значение не извлечено моделью, подставлено найденное "
                    f"правилами ({_format_amount(fallback.amount)})"
                )
        elif critical:
            summary.warnings.append(
                "Сумма контракта не найдена: проверьте, что в файл включён раздел "
                "с НМЦК (часто он лежит в отдельном обоснования цены)"
            )
        return

    if not _amount_in_text(value.amount, digits):
        summary.warnings.append(
            f"{label}: значение {_format_amount(value.amount)} не найдено в тексте "
            "документа дословно - проверьте по исходному файлу"
        )
        if fallback is not None and not _same_amount(value.amount, fallback.amount):
            summary.warnings.append(
                f"{label}: правила нашли в тексте другое значение - "
                f"{_format_amount(fallback.amount)}"
            )
        return

    if (
        fallback is not None
        and fallback.amount is not None
        and not _same_amount(value.amount, fallback.amount)
    ):
        summary.warnings.append(
            f"{label}: модель и правила расходятся ({_format_amount(value.amount)} "
            f"против {_format_amount(fallback.amount)}) - сверьте с документом"
        )

    if value.includes_vat is None and fallback is not None:
        value.includes_vat = fallback.includes_vat
    if not value.raw and fallback is not None:
        value.raw = fallback.raw


def _fill_scalar(summary: TenderSummary, reference: TenderSummary, field: str) -> None:
    current = getattr(summary, field)
    reference_value = getattr(reference, field)
    if not reference_value:
        return
    if current and field == "customer" and _looks_like_party_role(current):
        setattr(summary, field, reference_value)
        return
    if not current:
        setattr(summary, field, reference_value)


def _looks_like_party_role(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value.strip().lower()).rstrip(":")
    role_names = {"подрядчик", "исполнитель", "поставщик", "заказчик", "участник"}
    return normalized in role_names


def _normalize_procurement_number(value: str | None) -> str | None:
    """Оставить номер ЕИС (18-20 цифр), если он спрятан в длинной цитате."""
    if not value:
        return None
    match = _EIS_NUMBER_RE.search(value)
    if match:
        return match.group(1)
    return value.strip()


def _digits_only_numbers(text: str) -> str:
    """Текст, в котором разряды чисел склеены: "12 480 500" → "12480500".

    Так сумма из ответа модели сопоставляется с текстом независимо от того,
    какими пробелами (обычными или неразрывными) разделены разряды в PDF.
    """
    return _DIGIT_GROUP_SEPARATOR_RE.sub("", text)


def _amount_in_text(amount: float | None, digits: str) -> bool:
    if amount is None:
        return False
    integer_part = int(abs(amount))
    variants = {str(integer_part)}
    if amount != integer_part:
        variants.add(f"{amount:.2f}".replace(".", ","))
        variants.add(f"{amount:.2f}")
    return any(variant in digits for variant in variants)


def _same_amount(first: float | None, second: float | None) -> bool:
    if first is None or second is None:
        return False
    return abs(first - second) <= _AMOUNT_TOLERANCE


def _format_amount(amount: float | None) -> str:
    if amount is None:
        return "-"
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")
