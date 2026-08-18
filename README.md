# Умный суммаризатор тендерной документации

Сервис на FastAPI: принимает PDF, скачанный с сайта госзакупок
(ЕИС, zakupki.gov.ru), и возвращает краткую структурированную выжимку:
**сумму контракта, сроки выполнения, ключевые требования к исполнителю и список
штрафов**. Факты извлекаются LLM (OpenAI, Anthropic или бесплатная локальная
модель через Ollama), затем суммы контракта и обеспечений сверяются с
исходным текстом документа.

Подробное описание логики и алгоритма в [docs/SOLUTION.md](docs/SOLUTION.md).

## Возможности

- Разбор PDF с текстовым слоем, включая таблицы штрафов и графики этапов.
- Четыре взаимозаменяемых LLM-провайдера: `openai`, `anthropic`, `ollama`,
  `stub` (правила, работает офлайн без ключей).
- Map-reduce по документу: длинная документация нарезается на фрагменты, из
  которых отбираются наиболее релевантные.
- Сверка сумм контракта и обеспечений с исходным текстом; предупреждения о
  галлюцинациях и расхождениях.
- Постобработка выжимки: этапы работ отделены от прочих сроков, в штрафах
  остаются денежные санкции, требования сортируются по квалификации.
- Прослеживаемость: у каждого требования и штрафа указаны страницы исходника.
- Оценка полноты (`confidence`) с учётом охвата документа и предупреждений.
- Страница для загрузки файла руками и автодокументация OpenAPI.

## Быстрый старт

```bash
git clone https://github.com/dalem-732/ai-pdf-processor.git
cd ai-pdf-processor
make install-dev          # venv + зависимости
cp .env.example .env      # при необходимости отредактируйте
make run                  # http://localhost:8000
```

Откройте <http://localhost:8000> - страница загрузки PDF, либо
<http://localhost:8000/docs> - интерактивная документация API.

По умолчанию используется провайдер `stub`: сервис работает без ключей, сети и
GPU, собирая выжимку регулярными правилами. Этого достаточно, чтобы проверить
весь путь запроса; для полноценного анализа подключите модель.

### Проверка одной командой

```bash
make smoke                # соберёт демонстрационный PDF и напечатает выжимку
make smoke ARGS=path.pdf  # или прогонит ваш файл
```

## Подключение модели

### Ollama - бесплатно и локально

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b-instruct
```

```bash
# .env
TENDER_LLM_PROVIDER=ollama
TENDER_OLLAMA_MODEL=qwen2.5:7b-instruct
TENDER_OLLAMA_BASE_URL=http://localhost:11434
```

Проверено на `qwen2.5:7b-instruct` (устойчиво держит русский язык и формат
JSON) и `llama3.1:8b-instruct`.

### OpenAI

```bash
# .env
TENDER_LLM_PROVIDER=openai
TENDER_OPENAI_API_KEY=sk-...
TENDER_OPENAI_MODEL=gpt-4o-mini
```

`TENDER_OPENAI_BASE_URL` позволяет использовать любой OpenAI-совместимый
эндпоинт: Azure OpenAI, vLLM, LM Studio, OpenRouter.

### Anthropic

```bash
# .env
TENDER_LLM_PROVIDER=anthropic
TENDER_ANTHROPIC_API_KEY=sk-ant-...
TENDER_ANTHROPIC_MODEL=claude-3-5-sonnet-latest
# Для Sonnet 4.6+, Opus 4.7+ и Sonnet 5 клиент сам отключает temperature
# и assistant-prefill - см. app/llm/anthropic_client.py
```

## API

### `POST /api/v1/summarize`

Принимает `multipart/form-data`.

| Поле | Тип | Обязательно | Описание |
|------|-----|-------------|----------|
| `file` | файл | да | PDF тендерной документации |
| `provider` | строка | нет | Переопределить провайдера на этот запрос |
| `max_chunks` | число | нет | Ограничить число фрагментов документа |

```bash
curl -X POST http://localhost:8000/api/v1/summarize \
  -F "file=@documentation.pdf" | jq
```

Фрагмент ответа:

```json
{
  "request_id": "c8a9e7455748",
  "document": {
    "filename": "documentation.pdf",
    "pages": 2,
    "characters": 4358,
    "extractor": "pdfplumber",
    "likely_scanned": false
  },
  "llm": {
    "provider": "ollama",
    "model": "qwen2.5:7b-instruct",
    "chunks_total": 1,
    "chunks_sent": 1,
    "calls": 1
  },
  "summary": {
    "subject": "капитальный ремонт системы вентиляции хирургического корпуса",
    "customer": "ГБУЗ "Городская клиническая больница № 7" г. Казань",
    "procurement_number": "0173100007726000045",
    "contract_price": {
      "amount": 12480500.0,
      "currency": "RUB",
      "includes_vat": true,
      "raw": "12 480 500,00 (двенадцать миллионов четыреста восемьдесят тысяч пятьсот) рублей"
    },
    "application_security": { "amount": 124805.0, "currency": "RUB" },
    "contract_security": { "amount": 624025.0, "currency": "RUB" },
    "deadlines": {
      "start": "01.03.2026",
      "end": "31.08.2026",
      "duration": "180 календарных дней",
      "stages": [
        { "name": "Этап 1 - демонтажные работы", "deadline": "30 календарных дней" }
      ]
    },
    "requirements": [
      {
        "category": "license",
        "text": "Наличие действующей лицензии на монтаж средств обеспечения пожарной безопасности",
        "mandatory": true,
        "source_pages": [1]
      }
    ],
    "penalties": [
      {
        "violation": "Просрочка выполнения этапа работ",
        "sanction": "Пеня за каждый день просрочки",
        "formula": "1/300 ключевой ставки ЦБ РФ от цены контракта",
        "source_pages": [2]
      }
    ],
    "warnings": [],
    "confidence": 1.0
  },
  "elapsed_ms": 120
}
```

Коды ошибок:

| Код | Значение |
|-----|----------|
| 413 | Файл превышает `TENDER_MAX_UPLOAD_MB` |
| 415 | Загружен не PDF |
| 422 | PDF не читается либо в нём нет текстового слоя (нужен OCR) |
| 502 | Провайдер LLM недоступен |
| 503 | Провайдер LLM не настроен (нет ключа) |

### `GET /api/v1/health`

```json
{
  "status": "ok",
  "version": "1.0.0",
  "provider": "ollama",
  "model": "qwen2.5:7b-instruct",
  "provider_ready": true,
  "detail": null
}
```

## Настройки

Все переменные читаются из окружения или `.env` с префиксом `TENDER_`
(полный список с комментариями в [.env.example](.env.example)).

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `TENDER_LLM_PROVIDER` | `stub` | `openai` / `anthropic` / `ollama` / `stub` |
| `TENDER_MAX_UPLOAD_MB` | `25` | Лимит размера загружаемого файла |
| `TENDER_CHUNK_CHARS` | `12000` | Размер фрагмента текста |
| `TENDER_CHUNK_OVERLAP_CHARS` | `800` | Перекрытие соседних фрагментов |
| `TENDER_MAX_CHUNKS` | `20` | Сколько фрагментов отправлять в модель |
| `TENDER_LLM_TIMEOUT_SECONDS` | `120` | Таймаут запроса к модели |
| `TENDER_LLM_MAX_RETRIES` | `2` | Повторы при восстановимых сбоях |
| `TENDER_LLM_TEMPERATURE` | `0.0` | Температура генерации |
| `TENDER_LLM_MAX_OUTPUT_TOKENS` | `4000` | Лимит ответа модели |
| `TENDER_LOG_LEVEL` | `INFO` | Уровень логирования |

## Docker

```bash
docker compose up --build          # сервис на http://localhost:8000
```

Для локальной модели поднимите Ollama на хосте и укажите
`TENDER_OLLAMA_BASE_URL=http://host.docker.internal:11434`.

## Railway

1. Запушьте репозиторий на GitHub.
2. В [Railway](https://railway.app/) создайте проект **Deploy from GitHub** и
   выберите репозиторий - платформа подхватит `Dockerfile`.
3. В **Variables** задайте провайдера и ключ (Ollama на Railway недоступен):

   ```bash
   TENDER_LLM_PROVIDER=openai
   TENDER_OPENAI_API_KEY=sk-...
   ```

   Без ключей сервис тоже поднимется на `stub` (правила, без сети).

4. После деплоя откройте публичный URL сервиса и проверьте
   `/api/v1/health`, затем UI на `/`.

   Для больших PDF имеет смысл увеличить `TENDER_LLM_TIMEOUT_SECONDS`
   (например, до `300`).

## Разработка

```bash
make test    # pytest, 114 тестов, без обращений к сети
make lint    # ruff
```

Структура проекта:

```
app/
  main.py          FastAPI: endpoints, лимиты, обработка ошибок
  config.py        настройки из окружения
  schemas.py       контракт ответа = схема для LLM = валидатор
  errors.py        ошибки приложения с HTTP-кодами
  web.py           страница загрузки файла
  pdf/extract.py   PDF → постраничный текст с таблицами
  llm/             провайдеры: openai, anthropic, ollama, stub
  core/
    pipeline.py    оркестрация всех шагов
    chunking.py    нарезка и отбор фрагментов
    prompts.py     системный промпт и шаблон запроса
    heuristics.py  извлечение фактов правилами
    json_utils.py  восстановление JSON из ответа модели
    merge.py       слияние частичных выжимок
    validation.py  сверка сумм с текстом, дополнение пропусков
docs/SOLUTION.md   логика и алгоритм решения
scripts/           генератор тестового PDF, smoke-проверка
tests/             107 тестов
```

## Ограничения

- Сканы без текстового слоя не обрабатываются: файл нужно предварительно
  прогнать через OCR (`ocrmypdf`, ABBYY FineReader).
- На вход принимается один PDF, а не ZIP с комплектом документации.
- Приложения в форматах XLSX/DOCX не анализируются.
- Выжимка не является юридическим заключением: ключевые условия перед подачей
  заявки нужно проверить по исходному документу (для этого в ответе есть номера
  страниц и дословные цитаты).

## Лицензия

Проект распространяется по лицензии **MIT**, полный текст в файле
[LICENSE](LICENSE). Коротко: код можно свободно использовать, изменять и
распространять, в том числе в коммерческих проектах, при сохранении текста
лицензии и указания авторства; программа поставляется "как есть", без гарантий.

Лицензии всех зависимостей проверены на совместимость с MIT и перечислены в
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). Там же условия
использования моделей: они в поставку не входят, и для облачных провайдеров
действуют их пользовательские соглашения.
