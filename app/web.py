"""Одностраничный интерфейс для загрузки PDF руками.

Страница нужна не для красоты: проверять API через curl с multipart-файлом
неудобно, а Swagger не показывает результат в читаемом виде. Оформление -
локально подключённый XP.css (без CDN), сервис остаётся работоспособным
в закрытом контуре без интернета.
"""

UPLOAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Суммаризатор тендерной документации</title>
<link rel="stylesheet" href="/static/vendor/xp.css">
<style>
  body {
    margin: 0;
    min-height: 100vh;
    padding: 28px 16px 48px;
    background-color: #3a7bd5;
    background-image:
      radial-gradient(ellipse 120% 45% at 72% 92%, #6db33f 0%, #4a9228 38%, transparent 58%),
      radial-gradient(ellipse 90% 35% at 18% 88%, #8fd44a 0%, #5fa832 42%, transparent 62%),
      linear-gradient(180deg, #1e58c8 0%, #4a90e8 38%, #9fd0ff 62%, #b8e06a 78%, #6db33f 100%);
  }
  .desktop {
    max-width: 920px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .app-window {
    width: 100%;
    max-width: 100%;
    overflow: hidden;
  }
  .window-body {
    overflow-x: auto;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .title-bar-text {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .document-subject {
    margin: 0 0 12px;
    font-weight: 700;
    line-height: 1.35;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .lead { margin: 0 0 12px; }
  .hint { margin: 10px 0 0; font-size: 11px; color: #444; }
  .field-row-actions {
    display: flex;
    justify-content: flex-end;
    margin-top: 12px;
  }
  .money { font-weight: 700; color: #006400; font-size: 14px; }
  .warn { color: #804000; }
  .error-text { color: #cc0000; }
  .hidden { display: none !important; }
  .tag {
    display: inline-block;
    padding: 0 6px;
    margin-right: 6px;
    border: 1px solid #808080;
    background: #fff;
    font-size: 11px;
    color: #333;
  }
  .summary-grid {
    display: grid;
    grid-template-columns: 190px 1fr;
    gap: 6px 12px;
    margin: 0;
  }
  .summary-grid dt { margin: 0; color: #444; }
  .summary-grid dd {
    margin: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  fieldset { margin-bottom: 12px; }
  fieldset table { width: 100%; border-collapse: collapse; font-size: 12px; }
  fieldset th, fieldset td {
    text-align: left;
    padding: 4px 6px;
    border: 1px solid #808080;
    vertical-align: top;
    background: #fff;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  fieldset th { background: #ece9d8; }
  .tree-view li {
    margin-bottom: 4px;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .load-progress { width: 100%; margin-top: 10px; }
  .progress-percent {
    margin: 6px 0 0;
    font-size: 12px;
    font-weight: 700;
    color: #000;
  }
  details { margin-top: 8px; }
  .json-dump {
    margin-top: 8px;
    max-width: 100%;
    overflow: hidden;
  }
  .json-dump pre {
    font-size: 11px;
    line-height: 1.4;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    max-width: 100%;
    box-sizing: border-box;
    margin: 0;
  }
</style>
</head>
<body>
<div class="desktop">
  <div class="window app-window" role="application" aria-label="Суммаризатор">
    <div class="title-bar">
      <div class="title-bar-text">Суммаризатор тендерной документации</div>
    </div>
    <div class="window-body">
      <p class="lead">PDF с госзакупок → сумма контракта, сроки, требования к исполнителю и штрафы.</p>
      <form id="form">
        <fieldset>
          <legend>Исходные данные</legend>
          <div class="field-row-stacked">
            <label for="file">PDF-файл документации</label>
            <input id="file" name="file" type="file" accept="application/pdf,.pdf" required>
          </div>
          <div class="field-row-stacked">
            <label for="provider">Провайдер LLM</label>
            <select id="provider" name="provider">
              <option value="">Из конфигурации сервиса</option>
              <option value="stub">stub (правила, без сети)</option>
              <option value="ollama">ollama (локальная модель)</option>
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
            </select>
          </div>
        </fieldset>
        <div class="field-row-actions">
          <button id="submit" type="submit">Получить выжимку</button>
        </div>
        <p class="hint">API: <code>POST /api/v1/summarize</code> · схема - <a href="/docs">/docs</a></p>
      </form>
    </div>
    <div class="status-bar">
      <p class="status-bar-field">Готово</p>
      <p class="status-bar-field">PDF → JSON</p>
    </div>
  </div>

  <div id="status" class="window app-window hidden" aria-live="polite">
    <div class="title-bar">
      <div class="title-bar-text">Обработка документа</div>
    </div>
    <div class="window-body" id="status-body"></div>
    <div class="status-bar">
      <p class="status-bar-field" id="progress-status">Подождите…</p>
    </div>
  </div>

  <div id="result" class="window app-window hidden"></div>
</div>

<script>
const form = document.getElementById('form');
const statusBox = document.getElementById('status');
const statusBody = document.getElementById('status-body');
const resultBox = document.getElementById('result');
const progressStatus = document.getElementById('progress-status');

const STAGE_LABELS = {
  extract: 'Извлечение текста из PDF',
  chunk: 'Подготовка фрагментов',
  llm: 'Обработка моделью',
  merge: 'Слияние результатов',
  verify: 'Сверка с документом',
  finish: 'Формирование выжимки',
};

const escape = (value) => String(value ?? '').replace(/[&<>"]/g, (ch) =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));

const money = (value) => {
  if (!value || value.amount === null || value.amount === undefined) return '-';
  const formatted = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2 }).format(value.amount);
  const vat = value.includes_vat === true ? ', включая НДС'
            : value.includes_vat === false ? ', без НДС' : '';
  return `${formatted} ${value.currency || 'RUB'}${vat}`;
};

function showProgressUI() {
  statusBody.innerHTML =
    '<p id="progress-message">Подготовка…</p>' +
    '<progress id="progress-bar" class="load-progress" max="100" value="0"></progress>' +
    '<p id="progress-percent" class="progress-percent">0 %</p>';
  progressStatus.textContent = '0 %';
}

function updateProgress(percent, stage, detail) {
  const label = STAGE_LABELS[stage] || stage;
  const message = detail ? `${label}: ${detail}` : label;
  const bar = document.getElementById('progress-bar');
  const messageEl = document.getElementById('progress-message');
  const percentEl = document.getElementById('progress-percent');
  if (bar) bar.value = percent;
  if (messageEl) messageEl.textContent = message;
  if (percentEl) percentEl.textContent = `${percent} %`;
  progressStatus.textContent = `${percent} %`;
}

async function consumeEventStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let splitAt;
    while ((splitAt = buffer.indexOf('\\n\\n')) >= 0) {
      const block = buffer.slice(0, splitAt);
      buffer = buffer.slice(splitAt + 2);
      for (const line of block.split('\\n')) {
        if (line.startsWith('data: ')) {
          onEvent(JSON.parse(line.slice(6)));
        }
      }
    }
  }
  if (buffer.trim()) {
    for (const line of buffer.split('\\n')) {
      if (line.startsWith('data: ')) {
        onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('submit');
  const data = new FormData();
  data.append('file', document.getElementById('file').files[0]);
  const provider = document.getElementById('provider').value;
  if (provider) data.append('provider', provider);

  button.disabled = true;
  resultBox.classList.add('hidden');
  statusBox.classList.remove('hidden');
  showProgressUI();

  try {
    const response = await fetch('/api/v1/summarize/stream', { method: 'POST', body: data });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        detail = payload.detail || payload.error || detail;
      } catch (_) {}
      statusBody.innerHTML =
        `<p class="error-text"><strong>Ошибка ${response.status}</strong></p>` +
        `<p>${escape(detail)}</p>`;
      progressStatus.textContent = 'Ошибка';
      return;
    }

    let finalPayload = null;
    await consumeEventStream(response, (event) => {
      if (event.type === 'progress') {
        updateProgress(event.percent, event.stage, event.detail);
      } else if (event.type === 'done') {
        finalPayload = event.payload;
        updateProgress(100, 'finish', 'Готово');
      } else if (event.type === 'error') {
        throw new Error(event.detail || event.error || 'Ошибка обработки');
      }
    });

    if (!finalPayload) {
      throw new Error('Сервер не вернул результат');
    }
    statusBox.classList.add('hidden');
    render(finalPayload);
  } catch (error) {
    statusBody.innerHTML =
      `<p class="error-text"><strong>Сбой запроса</strong></p><p>${escape(error.message)}</p>`;
    progressStatus.textContent = 'Ошибка';
  } finally {
    button.disabled = false;
  }
});

function render(payload) {
  const summary = payload.summary;
  const deadlines = summary.deadlines || {};
  const stages = (deadlines.stages || [])
    .map((stage) => `<li>${escape(stage.name)} - ${escape(stage.deadline || 'срок не указан')}</li>`).join('');
  const rawMentions = (deadlines.raw_mentions || [])
    .map((item) => `<li>${escape(item)}</li>`).join('');
  const requirements = (summary.requirements || [])
    .map((item) => `<li><span class="tag">${escape(item.category)}</span>${escape(item.text)}` +
      (item.mandatory ? '' : ' <span class="tag">желательное</span>') + '</li>').join('');
  const penalties = (summary.penalties || []).map((item) => `
    <tr>
      <td>${escape(item.violation)}</td>
      <td>${escape(item.sanction)}</td>
      <td>${escape(item.formula || '')}${item.amount ? '<br>' + escape(money(item.amount)) : ''}</td>
    </tr>`).join('');
  const warnings = (summary.warnings || [])
    .map((item) => `<li class="warn">${escape(item)}</li>`).join('');
  const confidence = Math.round((summary.confidence || 0) * 100);

  const subject = summary.subject || 'Предмет закупки не определён';
  resultBox.innerHTML = `
    <div class="title-bar">
      <div class="title-bar-text">Результат обработки</div>
    </div>
    <div class="window-body">
      <p class="document-subject">${escape(subject)}</p>
      <fieldset>
        <legend>Основные сведения</legend>
        <dl class="summary-grid">
          <dt>Заказчик</dt><dd>${escape(summary.customer || '-')}</dd>
          <dt>Номер закупки</dt><dd>${escape(summary.procurement_number || '-')}</dd>
          <dt>Сумма контракта</dt><dd class="money">${escape(money(summary.contract_price))}</dd>
          <dt>Обеспечение заявки</dt><dd>${escape(money(summary.application_security))}</dd>
          <dt>Обеспечение контракта</dt><dd>${escape(money(summary.contract_security))}</dd>
          <dt>Начало работ</dt><dd>${escape(deadlines.start || '-')}</dd>
          <dt>Окончание работ</dt><dd>${escape(deadlines.end || '-')}</dd>
          <dt>Длительность</dt><dd>${escape(deadlines.duration || '-')}</dd>
          <dt>Полнота выжимки</dt><dd>${confidence} % <span class="hint">(с учётом охвата документа)</span></dd>
        </dl>
        <progress max="100" value="${confidence}" class="load-progress" aria-label="Полнота выжимки"></progress>
      </fieldset>
      ${stages ? `<fieldset><legend>Этапы работ</legend><ul class="tree-view">${stages}</ul></fieldset>` : ''}
      ${rawMentions ? `<fieldset><legend>Прочие сроки</legend><ul class="tree-view">${rawMentions}</ul></fieldset>` : ''}
      ${requirements ? `<fieldset><legend>Требования к исполнителю</legend><ul class="tree-view">${requirements}</ul></fieldset>` : ''}
      ${penalties ? `<fieldset><legend>Штрафы и пени</legend><table><thead><tr><th>Нарушение</th><th>Санкция</th><th>Размер</th></tr></thead><tbody>${penalties}</tbody></table></fieldset>` : ''}
      ${warnings ? `<fieldset><legend>Предупреждения</legend><ul class="tree-view">${warnings}</ul></fieldset>` : ''}
      <fieldset>
        <legend>Обработка</legend>
        <dl class="summary-grid">
          <dt>Файл</dt><dd>${escape(payload.document.filename)} · ${payload.document.pages} стр. · ${payload.document.characters} симв. · ${escape(payload.document.extractor)}</dd>
          <dt>Модель</dt><dd>${escape(payload.llm.provider)} / ${escape(payload.llm.model)} · фрагментов ${payload.llm.chunks_sent} из ${payload.llm.chunks_total}</dd>
          <dt>Время</dt><dd>${payload.elapsed_ms} мс</dd>
        </dl>
      </fieldset>
      <fieldset class="json-dump">
        <legend>Полный JSON-ответ</legend>
        <pre>${escape(JSON.stringify(payload, null, 2))}</pre>
      </fieldset>
    </div>
    <div class="status-bar">
      <p class="status-bar-field">${escape(payload.llm.provider)} / ${escape(payload.llm.model)}</p>
      <p class="status-bar-field">${payload.elapsed_ms} мс</p>
    </div>`;
  resultBox.classList.remove('hidden');
}
</script>
</body>
</html>
"""
