.PHONY: install dev dev-backend dev-web dev-member-A dev-member-B dev-member-C check sandbox \
	seed approve-orders test-live gate-a live-personas e2e-hosted

install:
	cd backend && uv sync
	cd web && pnpm install

dev:
	$(MAKE) -j5 dev-backend dev-web dev-member-A dev-member-B dev-member-C

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

# Consortium members on :8101-8103. Each needs backend/var/members/<M>/list.csv (`make seed`).
dev-member-A dev-member-B dev-member-C:
	cd backend && uv run python -m app.consortium.member $(subst dev-member-,,$@)

dev-web:
	cd web && pnpm dev --port 3000

check:
	cd backend && uv run ruff check . ../workflow && uv run ruff format --check . ../workflow && uv run pytest -q && uv run python scripts/scan_logs.py
	cd web && pnpm lint && pnpm typecheck

sandbox:
	uv run scripts/check_sandbox.py

# Demo member lists (D7): ~300 synthetic entries each plus the persona seeds. Needs PERSONA_*_EMAIL in .env.
seed:
	cd backend && uv run python scripts/seed.py

# Live PayPal sandbox runs (ADR 0007): stop dev-backend first and keep `ngrok http 8000` running.
approve-orders:
	cd backend && uv run python scripts/approve_orders.py --count $(or $(COUNT),2)

test-live:
	cd backend && uv run pytest -m live -s

gate-a:
	cd backend && uv run python scripts/gate_a.py --end $(or $(END),capture)

# D4 + D7 live acceptance: one order per persona, approved in the browser, decided by the webhook alone.
live-personas:
	cd backend && uv run python scripts/live_personas.py

# D10 / Gate C: Ana, Ben and Cy through the hosted shop and PayPal pop-up. ARGS e.g. --persona ben --headed
e2e-hosted:
	uv run scripts/e2e_hosted.py $(ARGS)
