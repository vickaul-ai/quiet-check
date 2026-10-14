# Quiet Check: Engineering

_Last reviewed: 2026-10-13_

Interfaces are specified in [contracts.md](contracts.md); this file covers how the code is laid out and run.

## Stack

Python 3.12 + FastAPI (uv), AP3 SDK (`ap3[a2a]` and `ap3-functions` 1.2.2, pinned), SQLAlchemy 2 Core (ADR 0005), Next.js 16 + React 19 + Tailwind 4 (pnpm), Vercel AI SDK and LangChain behind
a model swap layer, SQLite locally and Postgres on Render. Details and model choices are in [brief.md](brief.md).

## Architecture

```
 Storefront + shopping agent ─┐                       ┌─> Member A :8101 ┐
 Merchant console + copilot ──┼─> Backend :8000 ──────┼─> Member B :8102 ├ AP3 PSI; lists never leave members
            (web :3000)       │   /risk  /payments    └─> Member C :8103 ┘
                              │   + AP3 initiator :8100 (serves our AgentCard)
                              │   /merchant /webhooks
                              │          │  ▲
                              │          ▼  │ webhooks
                              │     PayPal sandbox (Orders v2, authorisations)
```

## Data model

Tables `orders`, `checks`, `receipts`, `decisions`, `events`: see [contracts.md](contracts.md#database-tables). All five
are defined in `backend/app/db.py` (SQLAlchemy Core) and created at startup with `create_all`; JSON columns are JSONB on
Postgres (ADR 0005). D2 writes `checks`, `receipts` and `events`; D3 writes `orders` and `events` and fills
`decisions.resolution` on reviewer actions.

## AP3 risk check (D2)

`POST /risk/check` reads the three identifiers from the order row (never the request; ADR 0006), hashes them (`app/identifiers.py`), then the in-process initiator runs 9 PSI
sessions at once (3 members x 3 fields) with a 60 s overall timeout. Any failed session fails the whole check. A
`RecordingPeerClient` captures the 2 signed intents each session sends (keyed by a per-task context variable, so
concurrent sessions don't mix), and each receipt is verified before it is stored (`verify_receipt`). Member cards are
re-fetched once per check so a restarted member's current commitment is recorded. Members (`MemberAgent`) answer only allowlisted initiator URLs (`--initiator-url`), checked before the SDK fetches anything. PSI is CPU-bound on the member side,
so each member serves its 3 sessions one after another; with ~300-entry lists a check takes 2.6-2.8 s on Vic's Mac
(measured 2026-10-13, `uv run pytest -m slow -s`).

## PayPal payments (D3)

`app/paypal.py` is an async httpx client: OAuth client-credentials token cached until 60 s before expiry (refreshed once on
a 401), Orders v2 create/get/authorize, Payments v2 capture/void/reauthorize, and verify-webhook-signature (the raw body
is spliced into the request so PayPal checks exactly the bytes it signed). Every money-moving POST sends
`PayPal-Request-Id: <order_id>:<action>`. `app/payments.py` prices orders from `app/catalog.json` and enforces the status
guards (ADR 0007); repeats return the stored result from the `orders` row and the last matching system event.
`app/webhooks.py` verifies, stores, dedupes on `paypal_event_id` and applies the contract's actions inside one
transaction. The Agent Toolkit has no authorise or void tool, so it isn't used.

Tests: `tests/fake_paypal.py` is a stateful fake PayPal behind an httpx `MockTransport` (idempotency keys, 401 token
expiry, injected errors, webhook verification). `tests/standins.py` plays D4's part (status and decision rows). Live
sandbox runs share `tests/live.py` (serve the app on :8000 in a thread, the saved-orders file, waiting for webhooks).

## Key files

| Path | Holds |
| --- | --- |
| `backend/app/main.py` | `create_app(settings)`: lifespan creates the DB engine and starts the AP3 initiator |
| `backend/app/config.py` | Settings (`DATABASE_URL`, ports, member URLs, key paths, check timeout) |
| `backend/app/db.py` | The five tables (SQLAlchemy Core) and `make_engine` |
| `backend/app/identifiers.py` | Shared normalise-and-hash (contracts.md, "Identifier normalisation") |
| `backend/app/risk.py` | `POST /risk/check`: claim/rerun logic, receipts, events |
| `backend/app/catalog.json`, `catalog.py` | The 7 SKUs (source of truth) and `GET /catalog` |
| `backend/app/paypal.py` | PayPal REST client: token cache, orders, authorisations, webhook verification |
| `backend/app/payments.py` | `/payments`: create, authorise, capture, void; `store_payer_details` for D4 |
| `backend/app/webhooks.py` | `POST /webhooks/paypal` |
| `backend/scripts/approve_orders.py` | Creates sandbox orders, waits for approval, saves them for live runs |
| `backend/scripts/gate_a.py` | Gate A: saved order -> risk check -> authorise -> capture or void |
| `backend/app/consortium/member.py` | `MemberAgent` (initiator allowlist) and the entry point: `python -m app.consortium.member A` |
| `backend/app/consortium/initiator.py` | Initiator `PrivacyAgent` (:8100), intent capture, 9-session run, `verify_receipt` |
| `backend/app/consortium/common.py` | Keys, list files, commitments |
| `backend/app/errors.py`, `ids.py` | Error body; prefixed ULIDs |
| `backend/scripts/scan_logs.py` | Fails if any list entry or test identifier appears in a test-run log |
| `backend/var/` | Git-ignored: member lists and keys, initiator key, `test-run/` |
| `backend/tests/` | pytest suite; `test_risk_check.py` runs the 20 seeded cases against 3 real member processes; `test_payments.py` and `test_webhooks.py` use the fake PayPal; `test_live_paypal.py` is opt-in (`-m live`) |
| `web/app/` | Next.js App Router pages |
| `scripts/check_sandbox.py` | PayPal sandbox OAuth check (uv inline-script dependencies) |
| `prompts/` | LLM prompts, one file per task |
| `.github/workflows/ci.yml` | CI: ruff + pytest; eslint + type-check |

## External services

- PayPal sandbox REST app, two sandbox buyer accounts, and a webhook pointing at `/webhooks/paypal` (ngrok locally, Render later).
- Anthropic API.

## Runbook

- Local: `make install`, `make dev` (backend, web and Members A-C). Members need their list files first
  (`backend/var/members/<M>/list.csv`, written by D7's seed script); a member without one exits with a message. Expose webhooks with `ngrok http 8000`, then set the sandbox webhook URL to
  `https://<ngrok-host>/webhooks/paypal`.
- `web/AGENTS.md` is written by `next dev`; commit it as is.
- Tests write every log, plus the synthetic lists, to `backend/var/test-run/`; `make check` and CI then run
  `scripts/scan_logs.py`, which fails if any list entry or test identifier appears in a log.
- Demo-size timing: `cd backend && uv run pytest -m slow -s` (3 checks at ~300 entries per member).
- Live PayPal runs (ADR 0007): stop `make dev-backend`, run `ngrok http 8000` (sandbox webhook pointing at it), then
  `make approve-orders` (approve the printed links as a sandbox buyer), then `make test-live` (uses 2 saved orders:
  capture and void) or `make gate-a END=capture|void` (uses 1). Saved orders and the live DB are in `backend/var/live/`.
  Webhook-simulator events from the dashboard fail verification (PayPal can't sign them), so test with real orders.
  `QC_LIVE_PORT` (default 8000) and `QC_LIVE_WEBHOOK_TIMEOUT` (default 180 s) tune the live runs.

## AP3 SDK notes

The D2 spike results (API shapes, signing, performance) are summarised in ADR 0004. AP3 1.2.2 needs Python <3.14.
The SDK logs only session-ID prefixes, but `a2a`'s card resolver logs whole AgentCards at INFO (including each member's
commitment `data_hash`, a digest of the whole list, not an entry).

## Risks

- `/risk/check` has no authentication; keep it off the public surface or add auth in D9 (ADR 0006). The same goes for
  `/payments/*` authorise, capture and void: they enforce status guards, but `actor: "reviewer"` is caller-asserted, so
  anyone who can reach the backend can capture or void a held order. D9 adds auth for all of these (ADR 0007).

See the Risks table in the build plan; the code-level ones are AP3 SDK drift (D2 spike) and webhook delivery during the demo.
