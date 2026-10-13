.PHONY: install dev dev-backend dev-web check sandbox

install:
	cd backend && uv sync
	cd web && pnpm install

dev:
	$(MAKE) -j2 dev-backend dev-web

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

dev-web:
	cd web && pnpm dev --port 3000

check:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
	cd web && pnpm lint && pnpm typecheck

sandbox:
	uv run scripts/check_sandbox.py
