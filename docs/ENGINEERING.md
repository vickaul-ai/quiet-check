# Quiet Check: Engineering

_Last reviewed: 2026-10-12_

Interfaces are specified in [contracts.md](contracts.md); this file covers how the code is laid out and run.

## Stack

Python 3.12 + FastAPI (uv), AP3 SDK (D2), Next.js 16 + React 19 + Tailwind 4 (pnpm), Vercel AI SDK and LangChain behind
a model swap layer, SQLite locally and Postgres on Render. Details and model choices are in [brief.md](brief.md).

## Architecture

```
 Storefront + shopping agent ─┐                       ┌─> Member A :8101 ┐
 Merchant console + copilot ──┼─> Backend :8000 ──────┼─> Member B :8102 ├ AP3 PSI; lists never leave members
            (web :3000)       │   /risk  /payments    └─> Member C :8103 ┘
                              │   /merchant /webhooks
                              │          │  ▲
                              │          ▼  │ webhooks
                              │     PayPal sandbox (Orders v2, authorisations)
```

## Data model

Tables `orders`, `checks`, `receipts`, `decisions`, `events`: see [contracts.md](contracts.md#database-tables). Not built yet.

## Key files

| Path | Holds |
| --- | --- |
| `backend/app/main.py` | FastAPI app; routers added from D2 |
| `backend/tests/` | pytest suite |
| `web/app/` | Next.js App Router pages |
| `scripts/check_sandbox.py` | PayPal sandbox OAuth check (uv inline-script dependencies) |
| `prompts/` | LLM prompts, one file per task |
| `.github/workflows/ci.yml` | CI: ruff + pytest; eslint + type-check |

## External services

- PayPal sandbox REST app, two sandbox buyer accounts, and a webhook pointing at `/webhooks/paypal` (ngrok locally, Render later).
- Anthropic API.

## Runbook

- Local: `make install`, `make dev`. Expose webhooks with `ngrok http 8000`, then set the sandbox webhook URL to
  `https://<ngrok-host>/webhooks/paypal`.
- `web/AGENTS.md` is written by `next dev`; commit it as is.

## Risks

See the Risks table in the build plan; the code-level ones are AP3 SDK drift (D2 spike) and webhook delivery during the demo.
