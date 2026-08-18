PYTHON ?= python3
VENV   ?= .venv
BIN     = $(VENV)/bin

.PHONY: help venv install install-dev run test lint fmt smoke clean

help:
	@echo "make install-dev  - создать venv и установить зависимости для разработки"
	@echo "make run          - поднять API на http://localhost:8000 (docs: /docs)"
	@echo "make test         - прогнать pytest"
	@echo "make lint         - проверить код ruff"
	@echo "make smoke        - собрать тестовый PDF и прогнать его через /summarize (provider=stub)"

venv:
	test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip

install: venv
	$(BIN)/pip install -r requirements.txt

install-dev: venv
	$(BIN)/pip install -r requirements-dev.txt

run:
	$(BIN)/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	$(BIN)/pytest -q

lint:
	$(BIN)/ruff check app tests scripts

fmt:
	$(BIN)/ruff format app tests scripts

smoke:
	$(BIN)/python -m scripts.smoke_test $(ARGS)

clean:
	rm -rf .pytest_cache .ruff_cache tmp
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
