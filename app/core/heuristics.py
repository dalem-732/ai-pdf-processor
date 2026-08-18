"""Детерминированное извлечение фактов регулярными выражениями.

Модуль выполняет три роли, и ни одна из них не «замена LLM»:

1. **Проверка ответа модели.** Суммы контракта и обеспечений сверяются с текстом;
   если LLM вернула сумму, которой нет в тексте, добавляется предупреждение;
   если цену контракта не вернула - подставляется найденная правилами, с отметкой.
2. **Резервный режим.** Провайдер ``stub`` строит выжимку только на этих
   правилах: сервис остаётся работоспособным без ключей, сети и GPU, а тесты
   становятся детерминированными.
3. **Оценка релевантности чанков.** Ключевые слова из этого модуля дают
   скоринг, по которому выбираются части документа для отправки в модель.

Формулировки закупочной документации шаблонны (44-ФЗ жёстко задаёт
терминологию), поэтому правила бьют по цели чаще, чем можно ожидать.
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

PAGE_MARKER_TEMPLATE = "[[стр. {page}]]"
PAGE_MARKER_RE = re.compile(r"\[\[стр\.\s*(\d+)\]\]")

# --- Деньги ---------------------------------------------------------------

_NUMBER = r"\d{1,3}(?:[  \u00a0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CURRENCY = r"(?P<mult>тыс\.?|млн\.?|миллион\w*|тысяч\w*)?\s*(?P<cur>руб(?:\.|лей|ля|ль)?|₽|RUB)"
# Между числом и словом «рублей» в документации почти всегда стоит сумма
# прописью в скобках: "12 480 500,00 (двенадцать миллионов ...) рублей".
MONEY_RE = re.compile(
    rf"(?P<num>{_NUMBER})\s*(?:\([^)]{{0,300}}\)\s*)?{_CURRENCY}",
    re.IGNORECASE,
)
VAT_INCLUDED_RE = re.compile(r"включа\w*\s+НДС|с\s+учётом\s+НДС|с\s+учетом\s+НДС", re.I)
VAT_EXCLUDED_RE = re.compile(r"без\s+(?:учёта\s+|учета\s+)?НДС|НДС\s+не\s+облага", re.I)

PRICE_KEYWORDS = (
    "начальная (максимальная) цена контракта",
    "начальная максимальная цена контракта",
    "нмцк",
    "цена контракта составляет",
    "цена контракта:",
    "цена договора, определенная",
    "цена договора, определённая",
    "цена договора определена",
    "цена договора",
    "стоимость работ составляет",
    "цена договора составляет",
    "сумма контракта",
)
CONTRACT_SECURITY_KEYWORDS = (
    "обеспечение исполнения контракта",
    "обеспечения исполнения контракта",
    "обеспечение исполнения договора",
    "независимой гарантии, обеспечивающей",
    "независимая гарантия, обеспечивающая",
    "обеспечивающей надлежащее исполнение обязательств",
)
APPLICATION_SECURITY_KEYWORDS = (
    "обеспечение заявки",
    "обеспечения заявки",
)

# --- Сроки ----------------------------------------------------------------

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
TEXT_DATE_RE = re.compile(
    rf"«?\d{{1,2}}»?\s+(?:{'|'.join(MONTHS)})\s+\d{{4}}\s*(?:г\.?|года)?", re.I
)
DURATION_RE = re.compile(
    r"(?:в\s+течение\s+)?(\d{1,4})\s*(?:\([^)]{0,60}\)\s*)?"
    r"(календарн\w*|рабоч\w*)?\s*(дн\w*|месяц\w*|недел\w*)",
    re.I,
)
START_KEYWORDS = (
    "срок начала",
    "начало выполнения",
    "дата начала",
    "дата начала работ",
    "с даты заключения",
)
END_KEYWORDS = (
    "срок окончания",
    "окончание выполнения",
    "дата окончания",
    "завершения работ",
)
END_SKIP_MARKERS = ("имущественн", "санкц", "ответственност", "устранен", "дефект", "гарантийн")
STAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?P<name>(?:этап|период)\s*(?:№\s*)?\d+[^\n:.]{0,120})[:—-]\s*(?P<body>[^\n]{0,220})",
        re.I,
    ),
    re.compile(
        r"(?P<name>(?:перв\w*|втор\w*|трет\w*|\d+)\s+этап[^\n:]{0,160})[:—-]\s*(?P<body>[^\n]{0,220})",
        re.I,
    ),
    re.compile(
        r"(?im)(?:^|\n)\s*(?P<name>(?:проектирование|капитальн\w+\s+ремонт|выполнени\w+\s+работ)"
        r"[^\n:]{0,80})[:—-]\s*(?P<body>[^\n]{0,220})",
    ),
)

# --- Требования -----------------------------------------------------------

REQUIREMENT_SECTION_RE = re.compile(
    r"требовани\w*\s+к\s+(?:участник\w*|исполнител\w*|подрядчик\w*|поставщик\w*|"
    r"лиц\w*,?\s+осуществляющ\w*)",
    re.I,
)
REQUIREMENT_SECTION_END_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:ответственность\s+сторон|порядок\s+расчёт\w*|"
    r"порядок\s+расчет\w*|порядок\s+оплаты|приёмка|приемка|техническое\s+задание|"
    r"проект\s+контракта|штрафы|неустойки)",
    re.IGNORECASE | re.MULTILINE,
)
REQUIREMENT_TRIGGERS = (
    "наличие",
    "должен",
    "должна",
    "обязан",
    "не менее",
    "не ниже",
    "лицензи",
    "сро",
    "саморегулируем",
    "опыт",
    "квалификац",
    "аттестац",
    "штат",
    "специалист",
    "оборудован",
    "техник",
    "деклараци",
    "выписк",
    "свидетельств",
    "аккредитац",
    "допуск",
    "отсутствие",
    "требуется",
    "предъявляются требования",
)
CATEGORY_RULES: tuple[tuple[RequirementCategory, tuple[str, ...]], ...] = (
    (RequirementCategory.LICENSE, ("лицензи", "разрешени", "допуск", "аккредитац")),
    (
        RequirementCategory.QUALIFICATION,
        ("сро", "саморегулируем", "квалификац", "аттестац", "сертификац", "реестр специалистов"),
    ),
    (RequirementCategory.EXPERIENCE, ("опыт", "аналогичн", "исполненн", "ранее выполн")),
    (RequirementCategory.STAFF, ("штат", "специалист", "работник", "персонал", "инженер")),
    (RequirementCategory.EQUIPMENT, ("оборудован", "техник", "машин", "средства измерен", "поверк")),
    (
        RequirementCategory.FINANCIAL,
        ("обеспечен", "банковск", "гаранти", "задолженност", "финансов", "устойчивост"),
    ),
    (
        RequirementCategory.DOCUMENTS,
        ("деклараци", "выписк", "копи", "документ", "справк", "заявк", "свидетельств"),
    ),
)
OPTIONAL_MARKERS = ("желательно", "рекомендуется", "может быть", "приветствуется", "при наличии")
_PARTY_ROLE_NAMES = frozenset(
    {"подрядчик", "исполнитель", "поставщик", "заказчик", "участник", "генподрядчик"}
)
_REQUIREMENT_EXCLUDE_RE = re.compile(
    r"(?:^за\s+нарушен|(?:^|\s)пен[яи](?:\s|$)|штраф|неустойк|1\s*/\s*130|"
    r"уплат\w+\s+заказчик|начисля\w+\s+пен)",
    re.I,
)

# --- Ответственность ------------------------------------------------------

# Триггеры намеренно "сильные": слово "ответственность" само по себе стоит в
# каждом втором разделе документации и давало бы поток ложных штрафов.
PENALTY_TRIGGERS = (
    "пеня",
    "пени",
    "пеню",
    "пеней",
    "штраф",
    "неустойк",
    "удержание обеспечения",
    "реестр недобросовестных",
    "рнп",
)
NON_PENALTY_MARKERS = (
    "не может превышать",
    "общая сумма начисленных",
    "освобождается от",
    "не начисляются",
    "порядок начисления определяется",
)
SANCTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Пеня", ("пеня", "пени", "пеню")),
    ("Штраф", ("штраф",)),
    ("Неустойка", ("неустойк",)),
    ("Удержание обеспечения", ("удержание обеспечения", "обращение обеспечения")),
    ("Включение в РНП", ("реестр недобросовестных", "рнп")),
    ("Возмещение убытков", ("возмещ",)),
)
FORMULA_RE = re.compile(
    r"(?:\d+\s*/\s*\d+|\d+(?:[.,]\d+)?\s*%)[^.;\n]{0,140}", re.IGNORECASE
)

# Ключевые слова для скоринга релевантности чанков.
RELEVANCE_KEYWORDS: dict[str, float] = {
    "нмцк": 5.0,
    "начальная (максимальная) цена": 5.0,
    "цена контракта": 3.0,
    "цена договора": 3.0,
    "обеспечение исполнения": 2.5,
    "обеспечение заявки": 2.0,
    "срок выполнения": 3.0,
    "сроки выполнения": 3.0,
    "срок оказания": 2.5,
    "календарных дней": 2.0,
    "этап": 1.0,
    "требования к участник": 5.0,
    "требования к исполнител": 4.0,
    "лицензи": 2.0,
    "саморегулируем": 2.0,
    "опыт": 1.0,
    "ответственность сторон": 4.0,
    "штраф": 3.0,
    "пени": 3.0,
    "неустойк": 2.5,
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+(?=[А-ЯA-Z0-9«(])|\n+")
# Строка-продолжение абзаца не начинается с номера пункта, маркера списка,
# маркера страницы или строки таблицы - такие переносы склеивать нельзя.
_LIST_ITEM_START_RE = re.compile(
    # Номер пункта - либо многоуровневый ("4.7", "4.7."), либо одноуровневый с
    # точкой/скобкой ("5.", "5)"). Голое число с пробелом и номером пункта не
    # считается: строка "31 Федерального закона № 44-ФЗ..." - это продолжение
    # предыдущей строки, а не новый пункт.
    r"^(?:\d+(?:\.\d+)+\.?\s|\d+[.)]\s|[-•–—*]\s|\[\[стр\.|\[ТАБЛИЦА\]|Этап\s|Раздел\s|Приложение\s)",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?;:]$")
# Сокращения, после точки в которых предложение не заканчивается.
_ABBREVIATIONS = (
    "г", "гг", "ул", "д", "стр", "обл", "руб", "коп", "тыс", "млн", "ст", "п", "пп", "ч", "им", "т",
)
_ABBREVIATION_GUARD = "".join(rf"(?<!\b{abbr})" for abbr in _ABBREVIATIONS)
_SENTENCE_STOP_RE = re.compile(
    rf"{_ABBREVIATION_GUARD}\.\s+(?=[А-ЯЁA-Z0-9«])|\n"
)
_MULTIPLIERS = (
    (("млн", "миллион"), 1_000_000.0),
    (("тыс", "тысяч"), 1_000.0),
)


def strip_page_markers(text: str) -> str:
    return PAGE_MARKER_RE.sub(" ", text)


def reflow(text: str) -> str:
    """Склеить строки, разорванные вёрсткой PDF, в непрерывный текст.

    В PDF перенос строки - элемент оформления, а не границы мысли: "Опыт
    исполнения не менее двух контрактов за\\nпоследние три года" физически
    состоит из двух строк. Без склейки разбор по предложениям режет
    требования и штрафы на бессмысленные обрывки.

    Не склеиваются строки таблиц (``|``), маркеры страниц, нумерованные
    пункты и строки, оканчивающиеся знаком конца предложения, - там перенос
    несёт смысл.
    """
    lines = text.splitlines()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not result:
            result.append(stripped)
            continue
        previous = result[-1]
        joinable = bool(
            previous
            and stripped
            and "|" not in previous
            and "|" not in stripped
            and not _SENTENCE_END_RE.search(previous)
            and not _LIST_ITEM_START_RE.match(stripped)
            and not PAGE_MARKER_RE.match(stripped)
        )
        if joinable:
            result[-1] = f"{previous} {stripped}"
        else:
            result.append(stripped)
    return "\n".join(result)


def _boundary_regex(tokens: tuple[str, ...]) -> re.Pattern[str]:
    """Регулярка "любой из токенов начинается на границе слова".

    Подстрочный поиск в русском тексте даёт грубые ложные срабатывания:
    "сро" находится внутри "просрочки", "рнп" - внутри латиницы. Границу
    ставим только слева, чтобы работал поиск по основе слова ("лицензи" →
    "лицензия", "лицензии").
    """
    alternatives = "|".join(
        re.escape(token) for token in sorted(tokens, key=len, reverse=True)
    )
    return re.compile(rf"\b(?:{alternatives})", re.IGNORECASE)


REQUIREMENT_TRIGGERS_RE = _boundary_regex(REQUIREMENT_TRIGGERS)
PENALTY_TRIGGERS_RE = _boundary_regex(PENALTY_TRIGGERS)
OPTIONAL_MARKERS_RE = _boundary_regex(OPTIONAL_MARKERS)
CATEGORY_RULES_RE: tuple[tuple[RequirementCategory, re.Pattern[str]], ...] = tuple(
    (category, _boundary_regex(markers)) for category, markers in CATEGORY_RULES
)
SANCTION_RULES_RE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, _boundary_regex(markers)) for label, markers in SANCTION_RULES
)


def page_at(text: str, position: int) -> int | None:
    """Номер страницы, к которой относится позиция в размеченном тексте."""
    last: int | None = None
    for match in PAGE_MARKER_RE.finditer(text):
        if match.start() > position:
            break
        last = int(match.group(1))
    return last


def parse_amount(raw: str) -> float | None:
    cleaned = raw.replace("\u00a0", "").replace(" ", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _apply_multiplier(amount: float, multiplier_token: str | None) -> float:
    if not multiplier_token:
        return amount
    token = multiplier_token.lower()
    for prefixes, factor in _MULTIPLIERS:
        if any(token.startswith(prefix) for prefix in prefixes):
            return amount * factor
    return amount


def find_money(text: str) -> list[tuple[Money, int]]:
    """Все денежные величины текста как пары ``(Money, позиция)``."""
    results: list[tuple[Money, int]] = []
    for match in MONEY_RE.finditer(text):
        amount = parse_amount(match.group("num"))
        if amount is None:
            continue
        amount = _apply_multiplier(amount, match.group("mult"))
        window = text[max(0, match.start() - 160) : match.end() + 80]
        includes_vat: bool | None = None
        if VAT_INCLUDED_RE.search(window):
            includes_vat = True
        elif VAT_EXCLUDED_RE.search(window):
            includes_vat = False
        results.append(
            (
                Money(
                    amount=amount,
                    currency="RUB",
                    includes_vat=includes_vat,
                    raw=_squeeze(match.group(0)),
                ),
                match.start(),
            )
        )
    return results


def money_near_keywords(
    text: str, keywords: tuple[str, ...], *, window: int = 400
) -> Money | None:
    """Ближайшая после ключевой фразы денежная величина.

    Порядок "ключевая фраза → сумма" соответствует тому, как формулируются
    разделы документации ("Начальная (максимальная) цена контракта
    составляет 12 480 500,00 рублей"), поэтому ищем именно вперёд, а не в
    обе стороны: назад чаще попадается сумма из соседнего пункта.
    """
    lowered = text.lower()
    money_positions = find_money(text)
    if not money_positions:
        return None

    best: tuple[int, Money] | None = None
    for keyword in keywords:
        start = 0
        while (index := lowered.find(keyword, start)) != -1:
            start = index + len(keyword)
            for money, position in money_positions:
                distance = position - index
                if 0 <= distance <= window and (best is None or distance < best[0]):
                    best = (distance, money)
    return best[1] if best else None


def extract_deadlines(text: str) -> Deadlines:
    deadlines = Deadlines()
    lowered = text.lower()

    deadlines.start = _phrase_near(text, lowered, START_KEYWORDS)
    deadlines.end = _phrase_near(text, lowered, END_KEYWORDS, skip_markers=END_SKIP_MARKERS)
    duration_info = _best_duration(text)
    if duration_info:
        deadlines.duration, duration_sentence = duration_info
        deadlines.raw_mentions.append(_squeeze(duration_sentence))

    seen_stages: set[str] = set()
    for pattern in STAGE_PATTERNS:
        for match in pattern.finditer(text):
            name = _squeeze(match.group("name"))
            if re.search(r"(?i)\bсрок\b", name):
                continue
            key = re.sub(r"[^a-zа-я0-9]+", "", name.lower())[:80]
            if not name or key in seen_stages:
                continue
            seen_stages.add(key)
            body = _squeeze(match.group("body"))
            deadline = None
            date_match = DATE_RE.search(body) or TEXT_DATE_RE.search(body)
            if date_match:
                deadline = date_match.group(0)
            else:
                duration_match = DURATION_RE.search(body)
                if duration_match:
                    deadline = _squeeze(duration_match.group(0))
            deadlines.stages.append(Stage(name=name, deadline=deadline))

    if not deadlines.raw_mentions:
        sentence = _first_sentence_with(text, ("срок",))
        if sentence:
            deadlines.raw_mentions.append(_squeeze(sentence))
    return deadlines


def extract_requirements(text: str, *, limit: int = 25) -> list[Requirement]:
    """Требования к исполнителю из соответствующих разделов документа.

    Приоритет отдаётся тексту после заголовка "Требования к участнику
    закупки": там формулировки нормативные, а не пересказ. Если заголовка нет
    (частый случай для проектов контрактов), правила применяются ко всему
    тексту, но с более строгим фильтром длины.
    """
    scope = _requirement_scope(text)
    requirements: list[Requirement] = []
    seen: set[str] = set()

    for sentence, position in _sentences(scope):
        cleaned = strip_page_markers(sentence).strip()
        if not 30 <= len(cleaned) <= 600:
            continue
        lowered = cleaned.lower()
        if not REQUIREMENT_TRIGGERS_RE.search(lowered):
            continue
        if _REQUIREMENT_EXCLUDE_RE.search(lowered):
            continue
        if PENALTY_TRIGGERS_RE.search(lowered) and not any(
            marker in lowered
            for marker in ("сро", "лиценз", "гарант", "обеспечен", "квалификац", "опыт")
        ):
            continue
        key = _dedupe_key(cleaned)
        if key in seen:
            continue
        seen.add(key)
        page = page_at(text, position)
        requirements.append(
            Requirement(
                category=_classify_requirement(lowered),
                text=_squeeze(cleaned),
                mandatory=not OPTIONAL_MARKERS_RE.search(lowered),
                source_pages=[page] if page else [],
            )
        )
        if len(requirements) >= limit:
            break
    return requirements


def extract_penalties(text: str, *, limit: int = 25) -> list[Penalty]:
    penalties: list[Penalty] = []
    seen: set[str] = set()

    # Строки таблиц ("нарушение | мера | размер") дают самую чистую привязку
    # санкции к нарушению, поэтому разбираются отдельно и первыми.
    for line_start, line in _lines_with_offsets(text):
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 2 or not PENALTY_TRIGGERS_RE.search(line):
            continue
        if _looks_like_table_header(cells):
            continue
        violation = _clean_penalty_text(cells[0])
        sanction_cell = cells[1]
        size_cell = cells[2] if len(cells) > 2 else ""
        key = _dedupe_key(violation + sanction_cell)
        if not violation or key in seen:
            continue
        seen.add(key)
        page = page_at(text, line_start)
        penalties.append(
            Penalty(
                violation=_squeeze(violation),
                sanction=_squeeze(sanction_cell) or _detect_sanction(line.lower()),
                formula=_extract_formula(f"{sanction_cell} {size_cell}"),
                amount=_first_money(size_cell),
                source_pages=[page] if page else [],
            )
        )

    for sentence, position in _sentences(text):
        if "|" in sentence or "[ТАБЛИЦА]" in sentence:
            continue  # табличные строки уже разобраны выше и точнее
        cleaned = _clean_penalty_text(sentence).strip()
        lowered = cleaned.lower()
        if not 30 <= len(cleaned) <= 700:
            continue
        if not PENALTY_TRIGGERS_RE.search(lowered):
            continue
        if not _is_clause_start(cleaned):
            continue
        if any(marker in lowered for marker in NON_PENALTY_MARKERS):
            continue
        # Больше трёх упоминаний санкций в одном "предложении" - это склеенная
        # плоским текстом таблица, а не пункт договора.
        if len(PENALTY_TRIGGERS_RE.findall(lowered)) > 3:
            continue
        key = _dedupe_key(cleaned)
        if key in seen:
            continue
        seen.add(key)
        page = page_at(text, position)
        penalties.append(
            Penalty(
                violation=_squeeze(cleaned),
                sanction=_detect_sanction(lowered),
                formula=_extract_formula(cleaned),
                amount=_first_money(cleaned),
                source_pages=[page] if page else [],
            )
        )
        if len(penalties) >= limit:
            break
    return penalties[:limit]


def extract_subject(text: str) -> str | None:
    value = _value_after_label(
        text,
        (
            "предмет закупки",
            "предмет контракта",
            "предмет договора",
            "объект закупки",
            "наименование объекта закупки",
        ),
    )
    if value and len(value) >= 20 and not _looks_like_party_role(value):
        return value

    match = re.search(r"(?i)\bпредмет\s+договора\b", text)
    if not match:
        return value
    section = text[match.end() : match.end() + 5000]
    works = re.findall(
        r"(?i)(?:разработ\w+|выполн\w+|оказ\w+|постав\w+|провед\w+)[^.;\n]{15,220}",
        section,
    )
    if works:
        return _squeeze("; ".join(dict.fromkeys(_squeeze(item) for item in works[:3])))
    return value


def extract_customer(text: str) -> str | None:
    preamble = text[:25_000]
    org_match = re.search(
        r"(?is)"
        r"(?:некоммерческ\w+\s+организац\w+|государственн\w+\s+учрежден\w+|"
        r"федеральн\w+\s+(?:государственн\w+\s+)?учрежден\w+|"
        r"муниципальн\w+\s+учрежден\w+)\s+"
        r"«[^»]{10,220}»",
        preamble,
    )
    if org_match:
        return _squeeze(org_match.group(0))

    role_match = re.search(
        r"(?is)«([^»]{10,220})»[^»]{0,160}\(\s*далее\s*[–-]\s*заказчик",
        preamble,
    )
    if role_match:
        return _squeeze(f"«{role_match.group(1)}»")

    candidates: list[tuple[int, str]] = []
    lowered = text.lower()
    for label in ("заказчик", "государственный заказчик", "наименование заказчика"):
        for match in re.finditer(rf"\b{re.escape(label)}\s*[:—-]\s*", lowered):
            value = _extract_party_name(text[match.end() : match.end() + 600])
            if value:
                candidates.append((match.start(), value))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (_party_name_score(item[1]), item[0]))
    return candidates[0][1]


def extract_procurement_number(text: str) -> str | None:
    match = re.search(r"\b(\d{18,20})\b", strip_page_markers(text))
    if match:
        return match.group(1)
    match = re.search(r"№\s*([0-9А-Яа-я\-/]{6,30})", strip_page_markers(text))
    return match.group(1) if match else None


def heuristic_summary(text: str) -> TenderSummary:
    """Полная выжимка только на правилах - основа провайдера ``stub``.

    Текст предварительно проходит ``reflow``: без склейки перенесённых строк
    правила по предложениям работают по обрывкам фраз.
    """
    text = reflow(text)
    return TenderSummary(
        subject=extract_subject(text),
        customer=extract_customer(text),
        procurement_number=extract_procurement_number(text),
        contract_price=money_near_keywords(text, PRICE_KEYWORDS),
        contract_security=money_near_keywords(text, CONTRACT_SECURITY_KEYWORDS),
        application_security=money_near_keywords(text, APPLICATION_SECURITY_KEYWORDS),
        deadlines=extract_deadlines(text),
        requirements=extract_requirements(text),
        penalties=extract_penalties(text),
    )


def relevance_score(text: str) -> float:
    """Насколько часть документа похожа на носителя нужных нам фактов."""
    lowered = text.lower()
    score = sum(
        weight * min(lowered.count(keyword), 3)
        for keyword, weight in RELEVANCE_KEYWORDS.items()
    )
    if MONEY_RE.search(text):
        score += 2.0
    if DATE_RE.search(text):
        score += 1.0
    return score


# --- Вспомогательные функции ---------------------------------------------


def _squeeze(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\n\r-—:;")


def _dedupe_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", strip_page_markers(value).lower())[:160]


def _sentences(text: str) -> list[tuple[str, int]]:
    sentences: list[tuple[str, int]] = []
    position = 0
    for part in _SENTENCE_SPLIT_RE.split(text):
        if part is None:
            continue
        index = text.find(part, position)
        if index == -1:
            index = position
        sentences.append((part, index))
        position = index + len(part)
    return sentences


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        lines.append((offset, line.strip()))
        offset += len(line)
    return lines


def _first_sentence_with(text: str, keywords: tuple[str, ...]) -> str | None:
    for sentence, _ in _sentences(text):
        lowered = sentence.lower()
        if any(keyword in lowered for keyword in keywords):
            return sentence
    return None


def _phrase_near(
    text: str,
    lowered: str,
    keywords: tuple[str, ...],
    *,
    skip_markers: tuple[str, ...] = (),
) -> str | None:
    """Дата или формулировка условия рядом с ключевой фразой о сроке."""
    for keyword in keywords:
        start = 0
        while (index := lowered.find(keyword, start)) != -1:
            start = index + len(keyword)
            window = text[index : index + 300]
            date_match = DATE_RE.search(window) or TEXT_DATE_RE.search(window)
            if date_match:
                return date_match.group(0)
            sentence_end = re.search(r"[.;\n]", window)
            candidate = window[: sentence_end.start()] if sentence_end else window
            candidate = _squeeze(strip_page_markers(candidate))
            if candidate and not any(marker in candidate.lower() for marker in skip_markers):
                return candidate
    return None


def _requirement_scope(text: str) -> str:
    """Границы раздела с требованиями к участнику.

    Раздел закрывается первым заголовком другой темы ("Ответственность
    сторон", "Техническое задание"). Без этой границы в требования утекают
    штрафы и порядок расчётов: слова "должен" и "не менее" есть и там.
    """
    match = REQUIREMENT_SECTION_RE.search(text)
    if match:
        start = match.start()
        end = min(len(text), start + 12_000)
        boundary = REQUIREMENT_SECTION_END_RE.search(text, match.end())
        if boundary:
            end = min(end, boundary.start())
        return text[start:end]

    responsibility = re.search(
        r"(?im)^\s*(?:\d+(?:\.\d+)*\.?\s+)?ответственность\s+сторон",
        text,
    )
    return text[: responsibility.start()] if responsibility else text[:80_000]


def _classify_requirement(lowered: str) -> RequirementCategory:
    for category, pattern in CATEGORY_RULES_RE:
        if pattern.search(lowered):
            return category
    return RequirementCategory.OTHER


def _detect_sanction(lowered: str) -> str:
    for label, pattern in SANCTION_RULES_RE:
        if pattern.search(lowered):
            return label
    return "Ответственность"


def _extract_formula(text: str) -> str | None:
    match = FORMULA_RE.search(text)
    return _squeeze(match.group(0)) if match else None


def _first_money(text: str) -> Money | None:
    found = find_money(text)
    return found[0][0] if found else None


def _is_clause_start(text: str) -> bool:
    """Начинается ли строка как самостоятельный пункт, а не как обрывок фразы.

    При "сплющивании" таблиц в текст попадают осколки вроде "вине подрядчика
    удержание обеспечения" - они начинаются со строчной. Пункт или предложение
    в документе обычно начинается с цифры или заглавной буквы.
    """
    first = text.lstrip("\"'(")[:1]
    return bool(first) and (first.isdigit() or first.isupper())


def _looks_like_table_header(cells: list[str]) -> bool:
    joined = " ".join(cells).lower()
    return joined.startswith("нарушение") or "мера ответственности" in joined


def _value_after_label(text: str, labels: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for label in labels:
        for match in re.finditer(rf"\b{re.escape(label)}\s*[:—-]\s*", lowered):
            window = text[match.end() : match.end() + 400]
            stop = _SENTENCE_STOP_RE.search(window)
            value = _squeeze(strip_page_markers(window[: stop.start()] if stop else window))
            if len(value) >= 5 and not _looks_like_party_role(value):
                return value.rstrip(".")
    return None


def _extract_party_name(window: str) -> str | None:
    window = strip_page_markers(window)
    quoted = re.search(r"«[^»]{5,220}»", window)
    if quoted:
        return _squeeze(quoted.group(0))

    for line in window.splitlines():
        cleaned = _squeeze(line.strip().rstrip(":"))
        if len(cleaned) < 5 or _looks_like_party_role(cleaned):
            continue
        if len(cleaned) >= 12 or any(
            marker in cleaned.lower()
            for marker in ("фонд", "гбу", "му ", "ооо", "ао ", "пао", "учрежден")
        ):
            return cleaned.rstrip(".")
    return None


def _looks_like_party_role(value: str) -> bool:
    normalized = _squeeze(value).lower().rstrip(":")
    return normalized in _PARTY_ROLE_NAMES


def _party_name_score(name: str) -> tuple[int, int]:
    lowered = name.lower()
    if _looks_like_party_role(name):
        return (3, len(name))
    if "«" in name or any(
        marker in lowered for marker in ("фонд", "гбу", "учрежден", "ооо", "ао", "пао")
    ):
        return (0, len(name))
    return (1, len(name))


def _best_duration(text: str) -> tuple[str, str] | None:
    transfer_match = re.search(
        r"(?is)"
        r"(\d{1,4})\s*(?:\([^)]{0,60}\)\s*)?(?:календарн\w*\s*)?(?:дн\w*)"
        r"[^.;\n]{0,80}(?:передачи\s+объекта|капитальн\w+\s+ремонт)",
        text,
    )
    if transfer_match:
        duration = _squeeze(f"{transfer_match.group(1)} календарных дней")
        return duration, transfer_match.group(0)

    candidates: list[tuple[int, int, str, str]] = []
    keywords = ("срок выполнения", "срок оказания", "срок поставки", "в течение", "календарных")
    for sentence, _ in _sentences(text):
        lowered = sentence.lower()
        if not any(keyword in lowered for keyword in keywords):
            continue
        match = DURATION_RE.search(sentence)
        if not match:
            continue
        if any(marker in lowered for marker in ("окончательн", "расчет", "расчёт", "гаранти")):
            priority = 3
        elif "проектн" in lowered and "капитальн" not in lowered:
            priority = 2
        elif any(marker in lowered for marker in ("выполнен", "капитальн", "ремонт", "работ")):
            priority = 0
        else:
            priority = 1
        unit = (match.group(3) or "").lower()
        kind = (match.group(2) or "").lower()
        unit_word = "дней" if unit.startswith("дн") else _squeeze(unit)
        qualifier = f"{kind} " if kind else ""
        duration = _squeeze(f"{match.group(1)} {qualifier}{unit_word}")
        candidates.append((priority, len(sentence), duration, sentence))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    _, _, duration, sentence = candidates[0]
    return duration, sentence


def _clean_penalty_text(text: str) -> str:
    cleaned = strip_page_markers(text)
    cleaned = re.sub(r"\]\]", " ", cleaned)
    cleaned = re.sub(r"^\d+\]\]\s*", "", cleaned)
    return _squeeze(cleaned)
