.PHONY: up down test burst venv

up:            ## app + db in one command
	docker compose up --build

down:
	docker compose down -v

venv:
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

test:          ## needs a local Postgres; see tests/conftest.py
	.venv/bin/python -m pytest tests/ -q

burst:         ## correctness gate against a running service (BASE_URL=...)
	.venv/bin/python scripts/burst.py $${BASE_URL:-http://localhost:8000}
