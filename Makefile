.PHONY: run test lint seed migrate handoff

ifeq ($(OS),Windows_NT)
  VENV_PY := .venv/Scripts/python.exe
else
  VENV_PY := .venv/bin/python
endif

ifeq ($(wildcard $(VENV_PY)),)
  PYTHON ?= python
else
  PYTHON ?= $(VENV_PY)
endif

run:
	$(PYTHON) -m uvicorn catalogguard.infrastructure.api.app:app --host 0.0.0.0 --port 8000

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

seed:
	$(PYTHON) -m catalogguard.seed

migrate:
	$(PYTHON) -m catalogguard.migrate

handoff:
	$(PYTHON) -m catalogguard.mailer
