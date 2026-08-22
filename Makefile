.PHONY: check install test typecheck lint compose-up compose-down

install:
	uv sync --frozen

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .
	node --check chrome-extension/background.js
	node --check chrome-extension/content-capture.js
	node --check chrome-extension/sidepanel.js

typecheck:
	uv run mypy

test:
	uv run pytest

compose-up:
	docker compose up --build

compose-down:
	docker compose down
