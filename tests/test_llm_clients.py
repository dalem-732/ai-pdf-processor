"""Тесты транспорта и провайдеров LLM.

Реальные вызовы моделей в тестах не делаются: проверяются форма запроса,
разбор ответа и классификация ошибок - то, из-за чего интеграция ломается
чаще всего. HTTP-клиент подменяется фейком на уровне httpx.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config import LLMProvider, Settings
from app.llm import build_client
from app.llm.anthropic_client import AnthropicClient, _anthropic_uses_modern_api
from app.llm.base import LLMError, LLMRequest
from app.llm.ollama_client import OllamaClient
from app.llm.openai_client import OpenAIClient
from app.llm.stub_client import StubClient

Handler = Callable[[str, dict[str, Any], dict[str, str] | None], httpx.Response]

COMMON = {"timeout": 5.0, "temperature": 0.0, "max_output_tokens": 512}
REQUEST = LLMRequest(system="системный промпт", user="пользовательский промпт", document_text="текст")


class _FakeAsyncClient:
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], headers=None) -> httpx.Response:
        self.calls.append((url, json, headers))
        return self._handler(url, json, headers)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Подменяет httpx.AsyncClient и отдаёт доступ к перехваченным запросам."""
    captured: dict[str, Any] = {}

    def install(handler: Handler) -> dict[str, Any]:
        fake = _FakeAsyncClient(handler)
        captured["client"] = fake
        monkeypatch.setattr("app.llm.base.httpx.AsyncClient", lambda **_: fake)
        return captured

    return install


async def test_openai_request_shape_and_response_parsing(transport) -> None:
    def handler(url: str, payload: dict[str, Any], headers) -> httpx.Response:
        assert url.endswith("/chat/completions")
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["messages"][0]["role"] == "system"
        assert headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"subject": "ремонт"}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
            },
        )

    transport(handler)
    client = OpenAIClient(
        api_key="secret-key", base_url="https://api.openai.com/v1", model="gpt-4o-mini", **COMMON
    )

    result = await client.complete(REQUEST)

    assert result.text == '{"subject": "ремонт"}'
    assert (result.prompt_tokens, result.completion_tokens) == (1200, 300)


async def test_anthropic_prefill_brace_is_restored(transport) -> None:
    """Anthropic отвечает без открывающей скобки, её обязан вернуть клиент."""

    def handler(url: str, payload: dict[str, Any], headers) -> httpx.Response:
        assert url.endswith("/messages")
        assert payload["system"] == "системный промпт"
        assert payload["messages"][-1] == {"role": "assistant", "content": "{"}
        assert headers["anthropic-version"]
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '"subject": "ремонт"}'}],
                "usage": {"input_tokens": 900, "output_tokens": 120},
                "stop_reason": "end_turn",
            },
        )

    transport(handler)
    client = AnthropicClient(
        api_key="key", base_url="https://api.anthropic.com/v1", model="claude-3-5-sonnet-latest", **COMMON
    )

    result = await client.complete(REQUEST)

    assert result.text == '{"subject": "ремонт"}'
    assert result.prompt_tokens == 900


def test_anthropic_api_mode_depends_on_model() -> None:
    assert _anthropic_uses_modern_api("claude-3-5-sonnet-latest") is False
    assert _anthropic_uses_modern_api("claude-sonnet-4-20250514") is False
    assert _anthropic_uses_modern_api("claude-opus-4-7") is True
    assert _anthropic_uses_modern_api("claude-sonnet-4-6") is True


async def test_anthropic_modern_models_skip_temperature_and_prefill(transport) -> None:
    def handler(url: str, payload: dict[str, Any], headers) -> httpx.Response:
        assert "temperature" not in payload
        assert payload["messages"] == [{"role": "user", "content": REQUEST.user}]
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"subject": "ремонт"}'}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            },
        )

    transport(handler)
    client = AnthropicClient(
        api_key="key",
        base_url="https://api.anthropic.com/v1",
        model="claude-sonnet-4-6",
        **COMMON,
    )

    result = await client.complete(REQUEST)

    assert result.text == '{"subject": "ремонт"}'


async def test_ollama_uses_json_format_and_extended_context(transport) -> None:
    def handler(url: str, payload: dict[str, Any], headers) -> httpx.Response:
        assert url.endswith("/api/chat")
        assert payload["format"] == "json"
        assert payload["stream"] is False
        # Контекст по умолчанию (2048) обрезал бы половину фрагмента.
        assert payload["options"]["num_ctx"] >= 8192
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"subject": "поставка"}'},
                "prompt_eval_count": 2048,
                "eval_count": 256,
            },
        )

    transport(handler)
    client = OllamaClient(base_url="http://localhost:11434", model="qwen2.5:7b-instruct", **COMMON)

    result = await client.complete(REQUEST)

    assert result.text == '{"subject": "поставка"}'
    assert result.completion_tokens == 256


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (401, False), (404, False), (400, False)],
)
async def test_http_errors_classified_for_retry(transport, status: int, retryable: bool) -> None:
    """Повторять осмысленно только троттлинг и сбои сервера."""
    transport(lambda *_: httpx.Response(status, text="ошибка провайдера"))
    client = OllamaClient(base_url="http://localhost:11434", model="model", **COMMON)

    with pytest.raises(LLMError) as info:
        await client.complete(REQUEST)

    assert info.value.retryable is retryable


async def test_timeout_is_retryable_error(transport) -> None:
    def handler(*_: object) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    transport(handler)
    client = OllamaClient(base_url="http://localhost:11434", model="model", **COMMON)

    with pytest.raises(LLMError, match="Таймаут") as info:
        await client.complete(REQUEST)

    assert info.value.retryable is True


async def test_non_json_body_raises_llm_error(transport) -> None:
    transport(lambda *_: httpx.Response(200, text="<html>502 Bad Gateway</html>"))
    client = OllamaClient(base_url="http://localhost:11434", model="model", **COMMON)

    with pytest.raises(LLMError, match="не-JSON"):
        await client.complete(REQUEST)


async def test_unexpected_response_structure_raises(transport) -> None:
    transport(lambda *_: httpx.Response(200, json={"unexpected": "shape"}))
    client = OpenAIClient(api_key="k", base_url="https://api.openai.com/v1", model="m", **COMMON)

    with pytest.raises(LLMError, match="структура ответа"):
        await client.complete(REQUEST)


async def test_stub_client_builds_summary_without_network() -> None:
    client = StubClient(model="heuristic-stub", **COMMON)
    document = (
        "Начальная (максимальная) цена контракта составляет 2 000 000,00 рублей. "
        "Общий срок выполнения работ составляет 60 календарных дней."
    )

    result = await client.complete(LLMRequest(system="s", user="u", document_text=document))

    assert '"amount":2000000.0' in result.text.replace(" ", "")


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (LLMProvider.OPENAI, OpenAIClient),
        (LLMProvider.ANTHROPIC, AnthropicClient),
        (LLMProvider.OLLAMA, OllamaClient),
        (LLMProvider.STUB, StubClient),
    ],
)
def test_factory_builds_requested_provider(provider: LLMProvider, expected: type) -> None:
    settings = Settings(
        llm_provider=provider, openai_api_key="key", anthropic_api_key="key"
    )

    assert isinstance(build_client(settings), expected)


def test_factory_requires_api_key_for_cloud_provider() -> None:
    with pytest.raises(LLMError, match="TENDER_OPENAI_API_KEY"):
        build_client(Settings(llm_provider=LLMProvider.OPENAI, openai_api_key=None))
