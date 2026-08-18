"""Тесты восстановления JSON из "грязных" ответов модели.

Каждый случай ниже - это реально встречающееся поведение LLM, из-за которого
запрос был бы потерян: платный вызов впустую или минута ожидания локальной
модели.
"""

from __future__ import annotations

import pytest

from app.core.json_utils import JsonParseError, parse_json_object


def test_plain_json() -> None:
    assert parse_json_object('{"contract_price": {"amount": 100}}') == {
        "contract_price": {"amount": 100}
    }


def test_markdown_fence_and_trailing_comma() -> None:
    raw = '```json\n{"requirements": [1, 2,], "penalties": [],}\n```'

    assert parse_json_object(raw) == {"requirements": [1, 2], "penalties": []}


def test_prose_around_json_is_ignored() -> None:
    raw = 'Вот извлечённые факты:\n{"subject": "ремонт"}\nНадеюсь, это поможет!'

    assert parse_json_object(raw) == {"subject": "ремонт"}


def test_brace_inside_quoted_citation_does_not_break_balance() -> None:
    raw = '{"raw": "цена {в скобках} рублей", "amount": 5}'

    assert parse_json_object(raw)["raw"] == "цена {в скобках} рублей"


def test_python_literals_are_repaired() -> None:
    raw = '{"includes_vat": True, "formula": None, "mandatory": False}'

    assert parse_json_object(raw) == {
        "includes_vat": True,
        "formula": None,
        "mandatory": False,
    }


def test_response_truncated_by_token_limit_is_closed() -> None:
    """Обрыв на лимите токенов: частичный результат полезнее ошибки."""
    raw = '{"contract_price": {"amount": 12480500}, "penalties": [{"violation": "просрочка'

    parsed = parse_json_object(raw)

    assert parsed["contract_price"]["amount"] == 12480500
    assert parsed["penalties"][0]["violation"] == "просрочка"


def test_object_wrapped_in_array() -> None:
    assert parse_json_object('[{"subject": "поставка"}]') == {"subject": "поставка"}


@pytest.mark.parametrize("raw", ["", "   ", "Извините, я не могу помочь с этим запросом."])
def test_unparsable_response_raises(raw: str) -> None:
    with pytest.raises(JsonParseError):
        parse_json_object(raw)
