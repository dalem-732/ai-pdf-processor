"""Слияние частичных выжимок в одну и оценка её полноты.

Стадия reduce сделана детерминированной, на Python, а не вторым вызовом LLM.
Причины ровно три, и все практические:

* модель на слиянии теряет пункты - из двенадцати штрафов в ответе стабильно
  остаётся "основное", причём выбор "основного" меняется от запроса к запросу;
* появляется второй источник галлюцинаций уже над сгенерированными данными,
  где исходного текста для проверки нет;
* это лишний платный вызов и лишняя точка отказа на каждый документ.

Правила слияния скалярных полей: побеждает первое непустое значение (части
идут в порядке документа, а титульный лист и раздел о цене всегда ближе к
началу). Списки объединяются с дедупликацией, при которой из двух похожих
формулировок остаётся более информативная.
"""

from __future__ import annotations

import re

from app.schemas import (
    Deadlines,
    Money,
    Penalty,
    Requirement,
    RequirementCategory,
    Stage,
    TenderSummary,
)

MAX_REQUIREMENTS = 40
MAX_PENALTIES = 40
MAX_STAGES = 12
MAX_RAW_MENTIONS = 15
MAX_OPERATIONAL_REQUIREMENTS = 8

# Вес разделов при расчёте confidence: сумма контракта и сроки - то, зачем
# пользователь пришёл, поэтому их отсутствие бьёт по оценке сильнее всего.
_CONFIDENCE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("contract_price", 0.3),
    ("deadlines", 0.25),
    ("requirements", 0.25),
    ("penalties", 0.2),
)

_WORK_STAGE_RE = re.compile(
    r"(?:"
    r"(?:перв\w*|1[-\s]?й|1)\s*этап"
    r"|(?:втор\w*|2[-\s]?й|2)\s*этап"
    r"|(?:трет\w*|3[-\s]?й|3)\s*этап"
    r"|(?:четверт\w*|4[-\s]?й|4)\s*этап"
    r"|этап\s*(?:№\s*)?\d"
    r"|разработк\w+\s+проектн\w+\s+документ"
    r"|выполнени\w+\s+работ"
    r"|проектирование"
    r"|капитальн\w+\s+ремонт"
    r")",
    re.IGNORECASE,
)
_ADMIN_MILESTONE_RE = re.compile(
    r"(?:"
    r"возврат\s+обеспечен"
    r"|открытие\s+целев"
    r"|подключени\w+\s+к\s+систем"
    r"|согласован\w+\s+(?:пред)?проект"
    r"|предоставлен\w+\s+(?:друг|подтверж|документ|независим)"
    r"|направлен\w+\s+представител"
    r"|внесени\w+\s+исправлен"
    r"|устранени\w+\s+недостатков\s+проект"
    r"|приступлен\w+\s+к\s+2"
    r"|детализированн"
    r")\b",
    re.IGNORECASE,
)
_NON_MONETARY_SANCTION_RE = re.compile(
    r"(?:рнп|реестр\s+недобросовест|односторонн\w*\s+расторжен|включение\s+в\s+рнп)",
    re.IGNORECASE,
)
_CUSTOMER_VIOLATION_RE = re.compile(
    r"(?:несвоевременн\w+\s+оплат|обязанност\w+\s+заказчик)",
    re.IGNORECASE,
)
_GENERIC_DISCLAIMER_RE = re.compile(
    r"(?:расторжен\w+\s+не\s+освобождает|не\s+освобождает\s+от\s+ответственности)",
    re.IGNORECASE,
)
_MONETARY_SANCTION_RE = re.compile(
    r"(?:пен[яи]|штраф|неустойк|удержан\w*\s+обеспечен)",
    re.IGNORECASE,
)
_FORMULA_HINT_RE = re.compile(r"[\d/%]|1\s*/\s*\d+")
_QUALIFICATION_CATEGORIES = frozenset(
    {
        RequirementCategory.LICENSE,
        RequirementCategory.QUALIFICATION,
        RequirementCategory.EXPERIENCE,
        RequirementCategory.FINANCIAL,
        RequirementCategory.STAFF,
    }
)
_OPERATIONAL_MARKERS = (
    "видеофиксац",
    "мусор",
    "огражден",
    "скрытых работ",
    "антикоррупц",
    "немедленно известить",
    "приостановить работ",
    "перемена подрядчика",
    "охраны окружающей среды",
    "вывоз",
)


def merge_summaries(parts: list[TenderSummary]) -> TenderSummary:
    """Собрать одну выжимку из частичных результатов по фрагментам."""
    if not parts:
        return TenderSummary()
    if len(parts) == 1:
        return _limit(parts[0].model_copy(deep=True))

    merged = TenderSummary(
        subject=_first_text(part.subject for part in parts),
        customer=_first_text(part.customer for part in parts),
        procurement_number=_first_text(part.procurement_number for part in parts),
        contract_price=_best_money(part.contract_price for part in parts),
        contract_security=_best_money(part.contract_security for part in parts),
        application_security=_best_money(part.application_security for part in parts),
        deadlines=_merge_deadlines([part.deadlines for part in parts]),
        requirements=_merge_requirements([r for part in parts for r in part.requirements]),
        penalties=_merge_penalties([p for part in parts for p in part.penalties]),
        warnings=_unique_texts(w for part in parts for w in part.warnings),
    )
    return _limit(merged)


def refine_summary(summary: TenderSummary) -> TenderSummary:
    """Убрать типичный шум LLM: административные сроки в этапах, неденежные
    санкции в штрафах, рутинные обязанности в требованиях."""
    refined = summary.model_copy(deep=True)
    refined.deadlines = _refine_deadlines(refined.deadlines)
    refined.requirements = _refine_requirements(refined.requirements)
    refined.penalties = _refine_penalties(refined.penalties)
    return _limit(refined)


def score_confidence(
    summary: TenderSummary,
    *,
    chunks_sent: int | None = None,
    chunks_total: int | None = None,
) -> float:
    """Доля заполненных ключевых разделов с поправкой на охват документа.

    Это не "уверенность модели", а метрика полноты и надёжности выжимки:
    учитываются заполненность разделов, доля отправленных фрагментов и
    критичные предупреждения сверки.
    """
    filled = 0.0
    for field, weight in _CONFIDENCE_WEIGHTS:
        value = getattr(summary, field)
        if field == "deadlines":
            has_value = bool(value.start or value.end or value.duration or value.stages)
        elif field == "contract_price":
            has_value = bool(value and value.amount)
        else:
            has_value = bool(value)
        if has_value:
            filled += weight

    if (
        chunks_sent is not None
        and chunks_total is not None
        and chunks_total > 0
        and chunks_sent < chunks_total
    ):
        coverage = chunks_sent / chunks_total
        filled *= 0.55 + 0.45 * coverage

    critical_warnings = sum(
        1
        for warning in summary.warnings
        if any(
            marker in warning.lower()
            for marker in (
                "не найдено в тексте",
                "расходятся",
                "не извлечено моделью",
            )
        )
    )
    filled -= min(0.2, critical_warnings * 0.08)
    filled -= min(0.12, max(0, len(summary.warnings) - 1) * 0.02)
    return round(max(0.0, min(filled, 1.0)), 2)


def _limit(summary: TenderSummary) -> TenderSummary:
    summary.requirements = summary.requirements[:MAX_REQUIREMENTS]
    summary.penalties = summary.penalties[:MAX_PENALTIES]
    summary.deadlines.stages = summary.deadlines.stages[:MAX_STAGES]
    summary.deadlines.raw_mentions = summary.deadlines.raw_mentions[:MAX_RAW_MENTIONS]
    return summary


def _first_text(values) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def _best_money(values) -> Money | None:
    """Первая сумма с числом; при равенстве предпочитается запись с цитатой.

    Цитата (raw) важнее полноты прочих полей: именно она позволяет позже
    сверить сумму с текстом документа.
    """
    best: Money | None = None
    for value in values:
        if value is None or value.amount is None:
            continue
        if best is None:
            best = value
            continue
        if not best.raw and value.raw:
            best = value
    return best


def _merge_deadlines(parts: list[Deadlines]) -> Deadlines:
    merged = Deadlines(
        start=_first_text(part.start for part in parts),
        end=_first_text(part.end for part in parts),
        duration=_first_text(part.duration for part in parts),
        raw_mentions=_unique_texts(m for part in parts for m in part.raw_mentions),
    )

    stages: list[Stage] = []
    seen: set[str] = set()
    for part in parts:
        for stage in part.stages:
            key = _normalize(stage.name)
            if not key or key in seen:
                continue
            seen.add(key)
            stages.append(stage)
    merged.stages = stages
    return merged


def _merge_requirements(items: list[Requirement]) -> list[Requirement]:
    kept: list[Requirement] = []
    for item in sorted(items, key=lambda req: len(req.text), reverse=True):
        text = item.text.strip()
        if not text:
            continue
        key = _normalize(text)
        duplicate_of = _find_similar(key, [_normalize(req.text) for req in kept])
        if duplicate_of is not None:
            # Тот же пункт из перекрытия чанков: объединяем страницы, текст
            # оставляем более длинный (он пришёл раньше из-за сортировки).
            kept[duplicate_of].source_pages = _merge_pages(
                kept[duplicate_of].source_pages, item.source_pages
            )
            continue
        kept.append(item.model_copy(deep=True))
    return kept


def _merge_penalties(items: list[Penalty]) -> list[Penalty]:
    kept: list[Penalty] = []
    for item in sorted(items, key=lambda pen: _penalty_weight(pen), reverse=True):
        violation = item.violation.strip()
        if not violation:
            continue
        key = _normalize(violation)
        duplicate_of = _find_similar(key, [_normalize(pen.violation) for pen in kept])
        if duplicate_of is not None:
            existing = kept[duplicate_of]
            existing.source_pages = _merge_pages(existing.source_pages, item.source_pages)
            existing.formula = existing.formula or item.formula
            existing.amount = existing.amount or item.amount
            continue
        kept.append(item.model_copy(deep=True))
    return kept


def _penalty_weight(penalty: Penalty) -> tuple[int, int, int]:
    """Приоритет записи о штрафе: с формулой и суммой информативнее.

    Сортировка по этому весу гарантирует, что при дедупликации остаётся
    запись из таблицы штрафов (там есть и размер, и формула), а не её
    пересказ из вводного абзаца.
    """
    return (
        1 if penalty.formula else 0,
        1 if penalty.amount and penalty.amount.amount else 0,
        len(penalty.violation),
    )


def _find_similar(key: str, existing_keys: list[str]) -> int | None:
    """Индекс похожей записи среди уже отобранных.

    Полное совпадение нормализованных строк не годится: перекрытие чанков и
    разные вызовы модели дают тот же пункт то целиком, то усечённым. Поэтому
    дубликатом считается и вложенность одной формулировки в другую.
    """
    if not key:
        return None
    for index, other in enumerate(existing_keys):
        if not other:
            continue
        if key == other:
            return index
        shorter, longer = sorted((key, other), key=len)
        if len(shorter) >= 40 and shorter in longer:
            return index
    return None


def _merge_pages(first: list[int], second: list[int]) -> list[int]:
    return sorted({page for page in (*first, *second) if page})


def _unique_texts(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or not value.strip():
            continue
        key = _normalize(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def _normalize(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", value.lower())


def _is_work_stage(name: str) -> bool:
    if _WORK_STAGE_RE.search(name):
        return True
    normalized = _normalize(name)
    return normalized in {"этап1", "этап2", "этап3", "этап4", "1этап", "2этап", "3этап", "4этап"}


def _stage_dedupe_key(name: str) -> str:
    normalized = _normalize(name)
    for token in ("первыйэтап", "1этап", "этап1"):
        if token in normalized:
            return "stage:1"
    for token in ("второйэтап", "2этап", "этап2"):
        if token in normalized:
            return "stage:2"
    for token in ("третийэтап", "3этап", "этап3"):
        if token in normalized:
            return "stage:3"
    for token in ("четвертыйэтап", "4этап", "этап4"):
        if token in normalized:
            return "stage:4"
    return normalized[:100]


def _refine_deadlines(deadlines: Deadlines) -> Deadlines:
    work_stages: list[Stage] = []
    seen: dict[str, Stage] = {}
    extra_mentions: list[str] = []

    for stage in deadlines.stages:
        name = stage.name.strip()
        if not name:
            continue
        if _is_work_stage(name):
            key = _stage_dedupe_key(name)
            existing = seen.get(key)
            if existing is None or (not existing.deadline and stage.deadline):
                seen[key] = stage
            continue
        if _ADMIN_MILESTONE_RE.search(name):
            mention = f"{name} - {stage.deadline}" if stage.deadline else name
            extra_mentions.append(mention)
            continue
        if stage.deadline:
            extra_mentions.append(f"{name} - {stage.deadline}")
        else:
            extra_mentions.append(name)

    work_stages = list(seen.values())
    raw_mentions = _unique_texts([*deadlines.raw_mentions, *extra_mentions])
    return deadlines.model_copy(
        update={"stages": work_stages, "raw_mentions": raw_mentions}
    )


def _refine_requirements(items: list[Requirement]) -> list[Requirement]:
    qualification: list[Requirement] = []
    documents: list[Requirement] = []
    operational: list[Requirement] = []
    other: list[Requirement] = []

    for item in items:
        text_lower = item.text.lower()
        if item.category in _QUALIFICATION_CATEGORIES:
            qualification.append(item)
        elif item.category is RequirementCategory.DOCUMENTS and any(
            marker in text_lower
            for marker in ("сро", "лиценз", "гаранти", "обеспечен", "выписк", "реестр", "экспертиз")
        ):
            documents.append(item)
        elif any(marker in text_lower for marker in _OPERATIONAL_MARKERS):
            operational.append(item)
        else:
            other.append(item)

    kept = qualification + documents + other + operational[:MAX_OPERATIONAL_REQUIREMENTS]
    return kept[:MAX_REQUIREMENTS]


def _is_monetary_penalty(penalty: Penalty) -> bool:
    violation = penalty.violation.lower()
    sanction = penalty.sanction.lower()
    if _CUSTOMER_VIOLATION_RE.search(violation):
        return False
    if _GENERIC_DISCLAIMER_RE.search(violation):
        return False
    if _NON_MONETARY_SANCTION_RE.search(sanction):
        return False
    if penalty.amount and penalty.amount.amount is not None:
        return True
    if penalty.formula and _FORMULA_HINT_RE.search(penalty.formula):
        return True
    return bool(_MONETARY_SANCTION_RE.search(sanction))


def _refine_penalties(items: list[Penalty]) -> list[Penalty]:
    return [item for item in items if _is_monetary_penalty(item)][:MAX_PENALTIES]
