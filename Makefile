.PHONY: up down test burst venv lint check audit

PYTHON ?= .venv/bin/python

up:            ## app + db in one command
	docker compose up --build

down:
	docker compose down -v

venv:
	python3 -m venv .venv
	$(PYTHON) -m pip install pip==26.2.1
	$(PYTHON) -m pip install -c constraints-dev.txt -r requirements-dev.txt

test:          ## needs a local Postgres; see tests/conftest.py
	$(PYTHON) -m pytest tests/ -q

burst:         ## correctness gate against a running service (BASE_URL=...)
	$(PYTHON) scripts/burst.py $${BASE_URL:-http://localhost:8000}


lint:
	$(PYTHON) -m ruff check app tests scripts migrate.py
	$(PYTHON) -m ruff format --check app tests scripts migrate.py

check: lint test
	$(PYTHON) -m pip check

audit:
	$(PYTHON) -m pip_audit --strict --progress-spinner off
