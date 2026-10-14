# Quiet Check: Engineering

_Last reviewed: 2026-10-14_

Interfaces are specified in [contracts.md](contracts.md); this file covers how the code is laid out and run.

## Stack

Python 3.12 + FastAPI (uv), AP3 SDK (`ap3[a2a]` and `ap3-functions` 1.2.2, pinned), SQLAlchemy 2 Core (ADR 0005), Next.js 16 + React 19 + Tailwind 4 (pnpm), Vercel AI SDK v7 (`ai`, `@ai-sdk/anthropic`, `@ai-sdk/react`) and LangChain behind
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
`decisions.resolution` on reviewer actions; D4 writes `decisions` and moves order status.

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
expiry, injected errors, webhook verification). Live sandbox runs share `tests/live.py` (serve the app on :8000 in a
thread, start members, the saved-orders file, waiting for webhooks or an order status).

## Risk agent (D4) and demo data (D7)

`app/merchant.py` (ADR 0008): `process_order` reads the payer details from PayPal, claims the order with one conditional
update to `checking`, calls `run_risk_check` (`app/risk.py`), applies `app/policy.py`, gets the rationale from
`app/rationale.py`, then `decide_order`, which recomputes the policy from the stored receipts, writes the decision and
`decision.made` event, and calls the payment functions in `app/payments.py` (`authorize_order`,
`capture_authorization`; the HTTP endpoints wrap the same functions). The webhook receiver starts `process_order` as a
FastAPI background task after its `200`. `rationale.py` builds the model with LangChain
`init_chat_model(f"{LLM_PROVIDER}:{LLM_MODEL_FAST}")`, sends `prompts/risk_rationale.md` plus the JSON hit summary,
and falls back to a template after any error or 10 s. Settings take a `rationale_model` so tests inject a fake.

`app/personas.json` defines the personas; `app/demo_data.py` generates the lists from a fixed seed per member and
`scripts/seed.py` writes them. CI runs the personas end to end (`tests/test_merchant.py`: real member processes with
the persona seeds, fake PayPal, fake model). Tests without members use `tests/stored_checks.py`, which writes a
completed check so the real `/merchant/process` skips PSI.

## Storefront and shopping agent (D5)

ADR 0009. `app/page.tsx` (a server component) reads `GET /catalog`, the personas and `PAYPAL_CLIENT_ID` per request and
renders `components/storefront.tsx`: persona switcher, product grid with Add buttons, cart, and the chat panel
(`useChat` from `@ai-sdk/react`). The cart is React state; each chat request sends `{persona, cart}` with the messages,
and the page adopts the cart from each `add_to_cart` tool output and clears it after `checkout`. Switching persona
remounts the shop, so chat and cart start fresh.

`app/api/chat/route.ts` runs `streamText` (AI SDK v7) with `prompts/shopping_agent.md`, the model from `lib/model.ts`
(the only place a provider is created) and the tools from `lib/agent.ts`, up to 6 steps. The tools close over the
request's cart and the persona's device ID (looked up server-side in `backend/app/personas.json`).
`components/paypal-checkout.tsx` loads the PayPal JS SDK v6 (sandbox `web-sdk/v6/core`), creates one SDK instance per
page with the client ID and one one-time payment session per order, and starts it in `popup` mode with the agent's
PayPal order ID. `onApprove` calls `/api/orders/{id}/process`, then polls `/status` until a final status and shows the
buyer text from `lib/types.ts`. `lib/backend.ts` is the only code that calls the backend; `lib/env.ts` loads the
repo-root `.env`. Product images are SVGs in `web/public/products/`.

## Merchant console and review copilot (D6)

ADR 0010. `app/console/page.tsx` renders `components/console.tsx`, a client component that reads only the
`/api/console/*` routes. It pages `/api/console/events` to the newest event on load, then polls every 2 s; any new event
refreshes the orders table and the open order, so a PayPal webhook shows up within about 2 s. The orders list also
refreshes every 10 s. `lib/copilot.ts` strips payer email and address from the order detail (`consoleOrder`), builds the
model's order summary and defines the copilot tools. `app/api/console/chat/route.ts` runs `streamText` with
`prompts/review_copilot.md` and `toolApproval` on the two money tools, signed with `experimental_toolApprovalSecret`
(`TOOL_APPROVAL_SECRET`, or a random per-process key). `components/copilot.tsx` (`useChat`, keyed on the open order)
renders the Approve/Deny card and sends the response back with `sendAutomaticallyWhen:
lastAssistantMessageIsCompleteWithApprovalResponses`; the tool runs on that second request.

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
| `backend/app/webhooks.py` | `POST /webhooks/paypal`; starts the risk agent on `CHECKOUT.ORDER.APPROVED` |
| `backend/app/merchant.py` | `/merchant`: process, decide, orders, order detail and status, events |
| `backend/app/policy.py`, `rationale.py` | The deterministic policy; the model rationale and its template fallback |
| `backend/app/personas.json`, `personas.py`, `demo_data.py` | Personas (device IDs, seeds, expected outcomes); list generation |
| `backend/scripts/seed.py` | `make seed`: writes the demo member lists |
| `backend/scripts/live_personas.py` | `make live-personas`: the three personas through the sandbox, webhook-driven |
| `backend/scripts/approve_orders.py` | Creates sandbox orders, waits for approval, saves them for live runs |
| `backend/scripts/gate_a.py` | Gate A: saved order -> risk agent -> capture, or hold -> reviewer void |
| `backend/app/consortium/member.py` | `MemberAgent` (initiator allowlist) and the entry point: `python -m app.consortium.member A` |
| `backend/app/consortium/initiator.py` | Initiator `PrivacyAgent` (:8100), intent capture, 9-session run, `verify_receipt` |
| `backend/app/consortium/common.py` | Keys, list files, commitments |
| `backend/app/errors.py`, `ids.py` | Error body; prefixed ULIDs |
| `backend/scripts/scan_logs.py` | Fails if any list entry or test identifier appears in a test-run log |
| `backend/var/` | Git-ignored: member lists and keys, initiator key, `test-run/` |
| `backend/tests/` | pytest suite; `test_risk_check.py` runs the 20 seeded cases against 3 real member processes; `test_payments.py` and `test_webhooks.py` use the fake PayPal; `test_merchant.py` runs the personas against real members; `test_live_paypal.py` is opt-in (`-m live`) |
| `web/app/page.tsx`, `web/components/storefront.tsx` | Storefront page (server) and its client UI: personas, products, cart, chat |
| `web/components/paypal-checkout.tsx` | PayPal JS SDK v6 button, `onApprove` -> process -> status polling, buyer status |
| `web/app/api/chat/route.ts`, `web/lib/agent.ts` | The shopping agent: `streamText` and its three tools |
| `web/app/api/orders/[orderId]/{process,status}/route.ts` | Server proxies for `/merchant/process` and the status endpoint |
| `web/app/console/page.tsx`, `web/components/console.tsx` | Merchant console: orders table, order detail (rationale, hit grid, receipts, who saw what), live event feed |
| `web/components/copilot.tsx`, `web/lib/copilot.ts`, `web/app/api/console/chat/route.ts` | Review copilot UI with the Approve/Deny card; its tools and route |
| `web/app/api/console/{orders,events}/` | Server proxies for the console's reads (payer details removed) |
| `web/lib/model.ts`, `env.ts`, `backend.ts`, `personas.ts`, `types.ts` | Model factory, root `.env` loading, backend client, persona loading, shared types and buyer text |
| `web/public/products/` | Product SVGs (referenced by `catalog.json`'s `image`) |
| `scripts/check_sandbox.py` | PayPal sandbox OAuth check (uv inline-script dependencies) |
| `prompts/` | LLM prompts, one file per task |
| `.github/workflows/ci.yml` | CI: ruff + pytest; eslint + type-check |

## External services

- PayPal sandbox REST app, three sandbox buyer accounts (one per persona), and two webhooks on the same app pointing at `/webhooks/paypal`: ngrok for local runs, the Render backend for the hosted demo (D9).
- Render: the Blueprint in `render.yaml` (backend, web, Members A-C, Postgres, the `quietcheck-risk` workflow); see [deploy-render.md](deploy-render.md) and ADR 0011.
- Anthropic API.

## Runbook

- Local: `make install`, `make seed` (needs `PERSONA_*_EMAIL` in `.env`), `make dev` (backend, web and Members A-C).
  A member without its list file exits with a message; after reseeding, restart the members. Expose webhooks with `ngrok http 8000`, then set the sandbox webhook URL to
  `https://<ngrok-host>/webhooks/paypal`.
- `web/AGENTS.md` is written by `next dev`; commit it as is.
- Storefront (D5): `make dev`, open http://localhost:3000, pick a persona and ask "buy the blue backpack in medium".
  Approve in the PayPal pop-up as that persona's sandbox buyer; log out at www.sandbox.paypal.com before switching
  persona (the pop-up reuses the last login). With ngrok running, the webhook and `onApprove` race; either is fine.
- Tests write every log, plus the synthetic lists, to `backend/var/test-run/`; `make check` and CI then run
  `scripts/scan_logs.py`, which fails if any list entry or test identifier appears in a log.
- Demo-size timing: `cd backend && uv run pytest -m slow -s` (3 checks at ~300 entries per member).
- Hosted (D9): `bash scripts/render_setup.sh` walks through the first deploy; `scripts/check_hosted.sh <backend> <web>`
  smoke-checks it. Details in [deploy-render.md](deploy-render.md). Run the backend suite on Postgres with
  `QC_TEST_POSTGRES_URL=postgresql://user@host:port/postgres uv run pytest` (CI does).
- Live PayPal runs (ADR 0007): stop `make dev-backend`, run `ngrok http 8000` (sandbox webhook pointing at it), then
  `make approve-orders` (approve the printed links as a sandbox buyer), then `make test-live` (uses 2 saved orders:
  capture and void) or `make gate-a END=capture|void` (uses 1). `approve-orders` runs with `process_on_approval` off, so
  saved orders stay `approved`. `make live-personas` needs no saved orders: it creates one order per persona and the
  APPROVED webhook drives each to its decision. Saved orders and the live DB are in `backend/var/live/`.
  Webhook-simulator events from the dashboard fail verification (PayPal can't sign them), so test with real orders.
  `QC_LIVE_PORT` (default 8000) and `QC_LIVE_WEBHOOK_TIMEOUT` (default 180 s) tune the live runs.

## AP3 SDK notes

The D2 spike results (API shapes, signing, performance) are summarised in ADR 0004. AP3 1.2.2 needs Python <3.14.
The SDK logs only session-ID prefixes, but `a2a`'s card resolver logs whole AgentCards at INFO (including each member's
commitment `data_hash`, a digest of the whole list, not an entry).

## Risks

- Locally, the backend, console and chats are open (no `BACKEND_API_TOKEN` or `CONSOLE_PASSWORD`). Hosted, the backend
  needs the token on everything but the webhook, health and status routes, the console needs its password, and the
  chats are rate-limited (D9, ADR 0011). `actor: "reviewer"` is still caller-asserted, so anyone with the token or the
  console password can capture or void a held order; that is the intended reviewer.
- The per-visitor chat limit trusts the first `X-Forwarded-For` entry; the global limit and the Anthropic spend limit
  bound the cost.

See the Risks table in the build plan; the code-level ones are AP3 SDK drift (D2 spike) and webhook delivery during the demo.
