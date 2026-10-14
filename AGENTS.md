# Quiet Check

_Last reviewed: 2026-10-13_

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
  reading `backend/var/members/<M>/list.csv` and `key.json` (git-ignored). DB: SQLAlchemy Core, tables in
  `backend/app/db.py` (ADR 0005). Integration tests start real members; `uv run pytest -m slow -s` times a demo-size check.
- Docs: `docs/PRODUCT.md`, `docs/ENGINEERING.md`, ADRs in `docs/decisions/`.
