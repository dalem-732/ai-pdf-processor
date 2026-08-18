"""Сквозная проверка сервиса на одной команде.

Собирает демонстрационный PDF, прогоняет его через пайплайн выбранным
провайдером и печатает выжимку в читаемом виде. Отдельный смысл скрипта -
проверить связку с реальной моделью там, где тестам делать это нельзя:

    TENDER_LLM_PROVIDER=ollama python scripts/smoke_test.py
    TENDER_LLM_PROVIDER=openai python scripts/smoke_test.py путь/к/файлу.pdf

Без аргументов и без настроек работает офлайн на провайдере stub.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Скрипт запускают и как `python -m scripts.smoke_test`, и напрямую. Во втором
# случае в sys.path попадает каталог scripts/, а не корень проекта.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.core.pipeline import summarize_pdf  # noqa: E402
from app.llm import build_client  # noqa: E402
from app.schemas import Money, TenderSummary  # noqa: E402
from scripts.make_sample_pdf import build_sample_pdf  # noqa: E402


def _money(value: Money | None) -> str:
    if value is None or value.amount is None:
        return "—"
    formatted = f"{value.amount:,.2f}".replace(",", " ").replace(".", ",")
    vat = ""
    if value.includes_vat is True:
        vat = ", включая НДС"
    elif value.includes_vat is False:
        vat = ", без НДС"
    return f"{formatted} {value.currency}{vat}"


def _print_summary(summary: TenderSummary) -> None:
    print(f"Предмет закупки : {summary.subject or '-'}")
    print(f"Заказчик        : {summary.customer or '-'}")
    print(f"Номер закупки   : {summary.procurement_number or '-'}")
    print(f"Сумма контракта : {_money(summary.contract_price)}")
    print(f"Обесп. заявки   : {_money(summary.application_security)}")
    print(f"Обесп. контракта: {_money(summary.contract_security)}")
    print(
        "Сроки           : "
        f"{summary.deadlines.start or '-'} → {summary.deadlines.end or '-'}"
        f" ({summary.deadlines.duration or 'длительность не указана'})"
    )
    for stage in summary.deadlines.stages:
        print(f"  этап: {stage.name} - {stage.deadline or 'срок не указан'}")

    print(f"\nТребования к исполнителю ({len(summary.requirements)}):")
    for item in summary.requirements:
        pages = f" [стр. {', '.join(map(str, item.source_pages))}]" if item.source_pages else ""
        print(f"  • [{item.category.value}] {item.text}{pages}")

    print(f"\nШтрафы и пени ({len(summary.penalties)}):")
    for item in summary.penalties:
        size = item.formula or _money(item.amount)
        print(f"  • {item.violation} → {item.sanction}: {size}")

    if summary.warnings:
        print(f"\nПредупреждения ({len(summary.warnings)}):")
        for warning in summary.warnings:
            print(f"  ! {warning}")
    print(f"\nПолнота выжимки : {summary.confidence:.0%}")


async def main() -> int:
    settings = get_settings()

    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
        if not pdf_path.is_file():
            print(f"Файл не найден: {pdf_path}", file=sys.stderr)
            return 1
    else:
        pdf_path = build_sample_pdf("tmp/sample_tender.pdf")
        print(f"Собран демонстрационный PDF: {pdf_path}")

    problem = settings.missing_credentials()
    if problem:
        print(f"Провайдер не готов: {problem}", file=sys.stderr)
        return 2

    print(
        f"Провайдер: {settings.llm_provider.value} / {settings.active_model}\n"
        f"Файл: {pdf_path} ({pdf_path.stat().st_size} байт)\n" + "-" * 70
    )

    result = await summarize_pdf(
        content=pdf_path.read_bytes(),
        filename=pdf_path.name,
        settings=settings,
        client=build_client(settings),
        request_id="smoke",
    )

    _print_summary(result.summary)
    print("-" * 70)
    print(
        f"Страниц: {result.document.pages}, символов: {result.document.characters}, "
        f"экстрактор: {result.document.extractor}"
    )
    print(
        f"Фрагментов отправлено: {result.llm.chunks_sent} из {result.llm.chunks_total}, "
        f"вызовов модели: {result.llm.calls}, токенов: "
        f"{result.llm.prompt_tokens or 0}+{result.llm.completion_tokens or 0}"
    )
    print(f"Время обработки: {result.elapsed_ms} мс")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
