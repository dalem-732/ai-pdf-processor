"""Генератор синтетической тендерной документации в PDF.

Реальные файлы с ЕИС нельзя положить в репозиторий (объём, лицензии,
персональные данные), а тестам нужен предсказуемый вход. Скрипт собирает
PDF, структурно похожий на документацию по 44-ФЗ: раздел с НМЦК, сроки,
требования к участнику и таблица штрафов с колонтитулами на каждой странице.

Запуск:
    python scripts/make_sample_pdf.py tmp/sample_tender.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

# Пути, по которым в Linux/macOS обычно лежит шрифт с кириллицей.
CYRILLIC_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)

DOCUMENT_TITLE = "ДОКУМЕНТАЦИЯ ОБ ЭЛЕКТРОННОМ АУКЦИОНЕ № 0173100007726000045"

SECTIONS: list[tuple[str, list[str]]] = [
    (
        "1. Общие сведения о закупке",
        [
            'Заказчик: ГБУЗ «Городская клиническая больница № 7» г. Казань.',
            "Предмет закупки: выполнение работ по капитальному ремонту системы "
            "вентиляции и кондиционирования хирургического корпуса.",
            "Способ определения поставщика: электронный аукцион в соответствии "
            "с Федеральным законом от 05.04.2013 № 44-ФЗ.",
            "Идентификационный код закупки (ИКЗ): 262166010773116010001000100014332244.",
        ],
    ),
    (
        "2. Начальная (максимальная) цена контракта",
        [
            "Начальная (максимальная) цена контракта составляет "
            "12 480 500,00 (двенадцать миллионов четыреста восемьдесят тысяч "
            "пятьсот) рублей 00 копеек, включая НДС 20 %.",
            "Источник финансирования: бюджет субъекта Российской Федерации на "
            "2026 год. Цена контракта является твёрдой и определяется на весь "
            "срок исполнения контракта.",
            "Размер обеспечения заявки на участие в закупке: 124 805,00 рублей "
            "(1 % начальной максимальной цены контракта).",
            "Размер обеспечения исполнения контракта: 624 025,00 рублей "
            "(5 % начальной максимальной цены контракта).",
        ],
    ),
    (
        "3. Сроки выполнения работ",
        [
            "Срок начала выполнения работ: с даты заключения контракта, но не "
            "ранее 01.03.2026.",
            "Срок окончания выполнения работ: 31.08.2026.",
            "Общий срок выполнения работ составляет 180 календарных дней с даты "
            "заключения контракта.",
            "Этап 1 - демонтажные работы и подготовка помещений: в течение 30 "
            "календарных дней с даты заключения контракта.",
            "Этап 2 - монтаж вентиляционного оборудования: до 30.06.2026.",
            "Этап 3 - пусконаладочные работы и сдача результата: до 31.08.2026.",
        ],
    ),
    (
        "4. Требования к участнику закупки (исполнителю)",
        [
            "4.1. Наличие действующей лицензии на осуществление деятельности по "
            "монтажу, техническому обслуживанию и ремонту средств обеспечения "
            "пожарной безопасности зданий и сооружений.",
            "4.2. Членство в саморегулируемой организации в области "
            "строительства с уровнем ответственности не ниже первого.",
            "4.3. Опыт исполнения не менее двух контрактов на выполнение "
            "аналогичных работ за последние три года, каждый на сумму не менее "
            "30 % начальной максимальной цены контракта.",
            "4.4. Наличие в штате не менее пяти специалистов, включённых в "
            "национальный реестр специалистов в области строительства.",
            "4.5. Наличие собственной или арендованной техники: автовышка, "
            "сварочное оборудование, средства измерения расхода воздуха с "
            "действующей поверкой.",
            "4.6. Отсутствие сведений об участнике в реестре недобросовестных "
            "поставщиков, отсутствие задолженности по налогам и сборам.",
            "4.7. Участник представляет в составе заявки декларацию соответствия "
            "требованиям статьи 31 Федерального закона № 44-ФЗ, копии "
            "лицензий и выписку из реестра членов СРО, выданную не ранее чем за "
            "месяц до даты подачи заявки.",
        ],
    ),
    (
        "5. Ответственность сторон",
        [
            "5.1. За каждый день просрочки исполнения подрядчиком обязательства, "
            "предусмотренного контрактом, начисляется пеня в размере одной "
            "трёхсотой действующей на дату уплаты пени ключевой ставки "
            "Центрального банка Российской Федерации от цены контракта, "
            "уменьшенной на сумму, пропорциональную объёму фактически "
            "исполненных обязательств.",
            "5.2. За неисполнение или ненадлежащее исполнение обязательств, не "
            "имеющих стоимостного выражения, подрядчик уплачивает штраф в "
            "размере 5 000,00 рублей.",
            "5.3. Общая сумма начисленных штрафов не может превышать цену "
            "контракта.",
        ],
    ),
]

PENALTY_TABLE = [
    ["Нарушение", "Мера ответственности", "Размер"],
    [
        "Просрочка выполнения этапа работ",
        "Пеня за каждый день просрочки",
        "1/300 ключевой ставки ЦБ РФ от цены контракта",
    ],
    [
        "Ненадлежащее качество работ",
        "Штраф",
        "10 % цены контракта (1 248 050,00 руб.)",
    ],
    [
        "Непредставление исполнительной документации",
        "Штраф",
        "5 000,00 руб. за каждый факт",
    ],
    [
        "Привлечение субподрядчика без согласования",
        "Штраф",
        "2 % цены контракта (249 610,00 руб.)",
    ],
    [
        "Расторжение контракта по вине подрядчика",
        "Включение в РНП и удержание обеспечения",
        "624 025,00 руб.",
    ],
]


def _resolve_font() -> str | None:
    for candidate in CYRILLIC_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def build_sample_pdf(path: str | Path) -> Path:
    """Собрать демонстрационный PDF и вернуть путь к нему."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font_path = _resolve_font()
    if font_path is None:
        raise RuntimeError(
            "Не найден TTF-шрифт с поддержкой кириллицы; "
            f"проверенные пути: {', '.join(CYRILLIC_FONT_CANDIDATES)}"
        )
    pdfmetrics.registerFont(TTFont("Cyr", font_path))

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CyrTitle",
        parent=styles["Title"],
        fontName="Cyr",
        fontSize=13,
        leading=17,
    )
    heading_style = ParagraphStyle(
        "CyrHeading",
        parent=styles["Heading2"],
        fontName="Cyr",
        fontSize=11,
        leading=15,
        spaceBefore=8,
    )
    body_style = ParagraphStyle(
        "CyrBody",
        parent=styles["BodyText"],
        fontName="Cyr",
        fontSize=9.5,
        leading=13,
        alignment=TA_JUSTIFY,
    )
    cell_style = ParagraphStyle(
        "CyrCell", parent=body_style, fontSize=8.5, leading=11, alignment=0
    )

    def draw_header_footer(canvas, doc) -> None:
        """Колонтитулы: проверяем, что парсер умеет их отбрасывать."""
        canvas.saveState()
        canvas.setFont("Cyr", 7)
        canvas.drawString(
            20 * mm, A4[1] - 12 * mm, "Извещение № 0173100007726000045 от 12.02.2026"
        )
        canvas.drawRightString(
            A4[0] - 20 * mm, 12 * mm, f"Страница {doc.page} из 4"
        )
        canvas.restoreState()

    story: list[object] = [Paragraph(DOCUMENT_TITLE, title_style), Spacer(1, 6 * mm)]
    for heading, paragraphs in SECTIONS:
        story.append(Paragraph(heading, heading_style))
        for paragraph in paragraphs:
            story.append(Paragraph(paragraph, body_style))
            story.append(Spacer(1, 2 * mm))

    story.append(Paragraph("6. Таблица штрафов и пеней", heading_style))
    table_data = [
        [Paragraph(cell, cell_style) for cell in row] for row in PENALTY_TABLE
    ]
    table = Table(table_data, colWidths=[55 * mm, 50 * mm, 60 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)

    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title="Документация об электронном аукционе",
    )
    document.build(story, onFirstPage=draw_header_footer, onLaterPages=draw_header_footer)
    return target


def main() -> int:
    destination = sys.argv[1] if len(sys.argv) > 1 else "tmp/sample_tender.pdf"
    path = build_sample_pdf(destination)
    print(f"PDF собран: {path.resolve()} ({path.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
