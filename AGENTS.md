# Quiet Check

_Last reviewed: 2026-10-14 (D9)_

A merchant agent privately checks AI-agent purchases against other merchants' fraud lists with AP3 PSI, then captures,
holds or declines the payment through PayPal Orders v2 (sandbox only).

- Python backend (`backend/`) uses uv; web (`web/`) uses pnpm. Root `Makefile`: `make dev`, `make check` (same as CI), `make sandbox`.
- Interfaces (endpoints, tables, normalisation, errors, idempotency) are fixed in `docs/contracts.md`. Change that file
  in the same change as any interface change.
- The decision policy is deterministic (`docs/brief.md`); the LLM writes the rationale and never decides or moves money.
- Models come from `LLM_PROVIDER`, `LLM_MODEL_FAST` and `LLM_MODEL_SMART`; prompts live in `prompts/*.md`. No
  provider-specific features in code that has to swap.
- Never log member list entries, normalised identifiers or their hashes. `make check` and CI scan the test-run logs.
- AP3: the backend hosts the initiator on :8100; Members A-C are separate processes (`make dev-member-A`, :8101-8103)
  reading `backend/var/members/<M>/list.csv` and `key.json` (git-ignored). Members answer only initiators on their `--initiator-url` allowlist; `/risk/check` reads identifiers from the order, never the request (ADR 0006). DB: SQLAlchemy Core, tables in
  `backend/app/db.py` (ADR 0005). Integration tests start real members; `uv run pytest -m slow -s` times a demo-size check.
- PayPal (sandbox): REST client in `backend/app/paypal.py`, no Agent Toolkit (it has no authorise/void). CI uses a fake
  PayPal; live runs (`make approve-orders`, `make test-live`, `make gate-a`) need a human to approve orders once and
  `ngrok http 8000` (ADR 0007). Payment endpoints enforce status guards; the risk agent (`app/merchant.py`, ADR 0008)
  owns status changes. Demo lists: `make seed`; persona emails live only in `.env`.
- Web (ADRs 0009, 0010): storefront at `/`, merchant console and review copilot at `/console`. The browser never calls the backend; the Next.js server does, at `BACKEND_URL`, and reads the
  repo-root `.env`. Next.js 16 and AI SDK v7 differ from older docs; read the bundled docs in `node_modules` first.
- Hosting (ADR 0011): Render Blueprint `render.yaml`, region oregon, created by Vic in the dashboard (no Render API key
  here); guide `docs/deploy-render.md`, wizard `scripts/render_setup.sh`. Hosted-only env vars (token gate, console
  password, AP3 keys, internal hostnames) are unset locally, which keeps local behaviour unchanged. The risk agent runs
  as Render Workflows task `quietcheck-risk/process_order` (`workflow/`) when `RENDER_API_KEY` is set. The judges' summary is plain HTML in `site/` (free static site `quietcheck-report`). Services stay up until Dec 16, 2026 (judging ends Dec 15).
- Docs: `docs/PRODUCT.md`, `docs/ENGINEERING.md`, ADRs in `docs/decisions/`.
