.PHONY: install dev dev-backend dev-web dev-member-A dev-member-B dev-member-C check sandbox

install:
	cd backend && uv sync
	cd web && pnpm install

dev:
	$(MAKE) -j5 dev-backend dev-web dev-member-A dev-member-B dev-member-C

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

# Consortium members on :8101-8103. Each needs backend/var/members/<M>/list.csv (D7 seeds them).
dev-member-A dev-member-B dev-member-C:
	cd backend && uv run python -m app.consortium.member $(subst dev-member-,,$@)

dev-web:
	cd web && pnpm dev --port 3000

check:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q && uv run python scripts/scan_logs.py
	cd web && pnpm lint && pnpm typecheck

sandbox:
	uv run scripts/check_sandbox.py
