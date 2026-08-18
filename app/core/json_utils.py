"""Извлечение JSON-объекта из ответа модели.

Даже с ``response_format=json_object`` и ``format=json`` ответ приходит
грязным чаще, чем хочется: markdown-ограждение, вступление "Вот результат:",
одинарные кавычки, висячая запятая перед закрывающей скобкой, обрыв генерации
на лимите токенов. Каждый такой случай - это либо потерянный запрос к платному
API, либо минута ожидания локальной модели, поэтому чинить ответ дешевле, чем
переспрашивать.

Порядок попыток - от самой безопасной к самой агрессивной; как только
получился словарь, останавливаемся.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class JsonParseError(ValueError):
    """Ответ модели не удалось привести к JSON-объекту."""


def parse_json_object(raw: str) -> dict:
    """Вернуть словарь из ответа модели или поднять ``JsonParseError``."""
    if not raw or not raw.strip():
        raise JsonParseError("Модель вернула пустой ответ")

    candidates = _candidates(raw)
    for candidate in candidates:
        parsed = _try_load(candidate)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            # Некоторые модели оборачивают результат в массив из одного объекта.
            for item in parsed:
                if isinstance(item, dict):
                    return item

    raise JsonParseError(
        f"Не удалось разобрать JSON в ответе модели: {raw[:300]!r}"
    )


def _candidates(raw: str) -> list[str]:
    text = _FENCE_RE.sub("", raw.strip())
    variants = [text]

    block = _largest_json_block(text)
    if block and block != text:
        variants.append(block)

    for variant in list(variants):
        repaired = _repair(variant)
        if repaired != variant:
            variants.append(repaired)
        closed = _close_unterminated(repaired)
        if closed != repaired:
            variants.append(closed)
    return variants


def _try_load(text: str) -> object | None:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _largest_json_block(text: str) -> str | None:
    """Наибольший сбалансированный фрагмент от первой ``{`` до парной ``}``.

    Отсекает и вступительный текст, и хвост вида "Надеюсь, это поможет!".
    Кавычки учитываются, иначе скобка внутри цитаты из документа ломает
    подсчёт баланса.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _repair(text: str) -> str:
    repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
    # Литералы Python/JS вместо JSON-совместимых значений.
    repaired = re.sub(r"\bNone\b", "null", repaired)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNaN\b", "null", repaired)
    return repaired


def _close_unterminated(text: str) -> str:
    """Дозакрыть структуру, обрезанную лимитом токенов.

    Обрыв на середине - типичный исход для локальной модели с маленьким
    num_predict. Частично разобранный объект (например, с ценой контракта, но
    без последних штрафов) полезнее, чем ошибка на весь документ.
    """
    in_string = False
    escaped = False
    stack: list[str] = []
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()

    if not stack and not in_string:
        return text

    repaired = text.rstrip()
    if in_string:
        repaired += '"'
    # Незавершённая пара "ключ:" или висячая запятая помешают разбору.
    repaired = re.sub(r",\s*$", "", repaired)
    repaired = re.sub(r'"[^"]*"\s*:\s*$', "", repaired).rstrip().rstrip(",")
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired
