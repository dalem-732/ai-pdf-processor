"""Тесты HTTP-слоя через TestClient.

Провайдер во всех тестах - stub, поэтому проверки идут без сети и без ключей.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def client(stub_settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: stub_settings
    return TestClient(app)


def test_health_reports_active_provider(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "stub"
    assert body["provider_ready"] is True


def test_index_page_available(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Суммаризатор тендерной документации" in response.text


def test_openapi_schema_documents_summarize(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/api/v1/summarize" in schema["paths"]
    assert "TenderSummary" in schema["components"]["schemas"]


def test_summarize_returns_all_requested_blocks(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    """Главный сценарий задания: сумма, сроки, требования, штрафы."""
    response = client.post(
        "/api/v1/summarize",
        files={"file": ("tender.pdf", sample_pdf_bytes, "application/pdf")},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["summary"]["contract_price"]["amount"] == pytest.approx(12_480_500.0)
    assert body["summary"]["deadlines"]["duration"] == "180 календарных дней"
    assert len(body["summary"]["requirements"]) >= 5
    assert len(body["summary"]["penalties"]) >= 5
    assert body["document"]["pages"] >= 1
    assert body["llm"]["provider"] == "stub"
    assert body["request_id"]


def test_summarize_stream_reports_progress(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    events: list[dict] = []
    with client.stream(
        "POST",
        "/api/v1/summarize/stream",
        files={"file": ("tender.pdf", sample_pdf_bytes, "application/pdf")},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            events.append(json.loads(line.removeprefix("data: ")))

    progress = [event for event in events if event["type"] == "progress"]
    done = [event for event in events if event["type"] == "done"]

    assert progress
    assert progress[0]["percent"] >= 0
    assert progress[-1]["percent"] == 100
    assert done
    assert done[0]["payload"]["summary"]["contract_price"]["amount"] == pytest.approx(
        12_480_500.0
    )


def test_request_id_echoed_from_header(client: TestClient, sample_pdf_bytes: bytes) -> None:
    response = client.post(
        "/api/v1/summarize",
        files={"file": ("tender.pdf", sample_pdf_bytes, "application/pdf")},
        headers={"X-Request-ID": "trace-42"},
    )

    assert response.headers["X-Request-ID"] == "trace-42"
    assert response.json()["request_id"] == "trace-42"


def test_provider_can_be_overridden_per_request(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    """Переопределение провайдера на запрос: удобно сравнить модели на одном файле."""
    response = client.post(
        "/api/v1/summarize",
        files={"file": ("tender.pdf", sample_pdf_bytes, "application/pdf")},
        data={"provider": "openai"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == "provider_not_configured"


def test_non_pdf_upload_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/summarize",
        files={"file": ("notes.txt", b"just text", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_file"


def test_broken_pdf_reported_as_unreadable(client: TestClient) -> None:
    response = client.post(
        "/api/v1/summarize",
        files={"file": ("broken.pdf", b"%PDF-1.4 broken", "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "unreadable_pdf"


def test_empty_upload_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/summarize", files={"file": ("empty.pdf", b"", "application/pdf")}
    )

    assert response.status_code == 415


def test_oversized_upload_rejected(sample_pdf_bytes: bytes) -> None:
    """Лимит размера проверяется на чтении, до разбора файла."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_provider="stub", max_upload_mb=1
    )
    client = TestClient(app)
    payload = b"%PDF-1.4" + b"0" * (2 * 1024 * 1024)

    response = client.post(
        "/api/v1/summarize", files={"file": ("huge.pdf", payload, "application/pdf")}
    )

    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"


def test_missing_file_is_validation_error(client: TestClient) -> None:
    assert client.post("/api/v1/summarize").status_code == 422
