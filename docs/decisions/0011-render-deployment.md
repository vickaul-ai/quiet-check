# 0011. Render deployment: Blueprint, private network, token gate, risk agent on Render Workflows
_Date: 2026-10-14 · Status: accepted_

## Context
D9 hosts the demo on Render for judges until Nov 30, 2026. Locally the backend's money and read endpoints, the console
and the chats are open, the AP3 parties find each other on `127.0.0.1`, keys and member lists live in the git-ignored
`backend/var/`, and the risk agent runs inside the backend. The Render sponsor prize asks for Render Workflows. Vic
creates the services in the dashboard (the session has no Render API key) and set the budget: about $41/month, AI spend capped
at $10.

## Decision
- **One Blueprint (`render.yaml`), one region (`oregon`)** so the private network joins everything. Decided with Vic:
  5 Starter services (backend and web as public web services; Members A, B, C as private services) and Basic-256mb
  Postgres (private only, `ipAllowList: []`), plus the workflow. Deploys wait for CI (`autoDeployTrigger: checksPass`).
- **Hostnames from the Blueprint, own URLs from `RENDER_SERVICE_NAME`.** AP3 requires the URL a member declares (its
  card URL) to equal the URL the initiator calls it at, and members accept only allowlisted initiator URLs. Each AP3
  party builds its own URL from Render's default `RENDER_SERVICE_NAME`; peers get it from `fromService ... property:
  host`. Render's docs show the two are the same name (the discovery name is both `$RENDER_SERVICE_NAME-discovery` and
  `<internal hostname>-discovery`) but don't say so outright, so `INITIATOR_URL`, `MEMBER_CARD_URL` and
  `MEMBER_<M>_URL` can override them without a code change.
- **Backend token gate.** Hosted, every backend route needs `Authorization: Bearer $BACKEND_API_TOKEN` except
  `/health`, the PayPal webhook (signature-verified) and the status-only endpoint. The backend refuses to start on
  Render without the token. The backend stays a public web service because PayPal must reach the webhook.
- **Console: HTTP Basic in `web/proxy.ts`** (Next.js 16's renamed middleware) on `/console` and `/api/console/*`, any
  username, password `CONSOLE_PASSWORD` (decided with Vic: one shared password in the Devpost testing instructions).
  The browser's own prompt needs no login page, and the console link is a plain `<a>` so the prompt appears.
- **Chat limits:** 20 requests per visitor per 10 minutes on each chat route, 600 per day across all visitors, in
  memory (one web instance), plus Vic's $10 spend limit on the Anthropic key's workspace. A spent budget shows a plain
  message instead of a generic error.
- **AP3 keys from env vars** (`INITIATOR_PRIVATE_KEY`, `MEMBER_PRIVATE_KEY`, `generateValue` secrets), because Render
  wipes the filesystem on deploy. A disk would cost money and stop zero-downtime deploys.
- **Member lists seeded at start** with `scripts/seed.py --member <M>`, which needs only the emails seeded on that
  list (A and C: Cy's; B: Ben's) and writes the same bytes as `make seed`, so commitments stay stable across deploys.
- **Risk agent as a Render Workflows task** (decided with Vic: not the PSI fan-out, since task runs can't receive
  connections and members call back to the initiator). The webhook and the storefront's `onApprove` (new
  `POST /merchant/start`) start `process_order` through Render's API with the order ID as idempotency key; the task
  calls `/merchant/process` over the private network, and Render retries it on a 5xx, which D4's rerun-and-resume
  design makes safe. Without `RENDER_API_KEY`, or if Render's API refuses, the risk agent runs in-process as before.
  A `risk_agent.started` event shows which runner took the order.
- **Postgres in CI:** the backend suite runs on SQLite and on Postgres 18 (`QC_TEST_POSTGRES_URL`).

## Alternatives considered
- Backend as a private service behind the web app, which would proxy the webhook: one less public surface, but more
  code in the web app and a webhook path through two services.
- A persistent disk for keys and lists: $0.25/GB/month and no zero-downtime deploys.
- Docker images: reproducible, but slower builds, and Docker-based workflows can only be created in the dashboard.
- Running the PSI fan-out as workflow tasks: impossible with AP3's callback to the initiator.

## Consequences
- Locally nothing changes unless the hosted variables are set; `make dev` and the tests run as before.
- Two triggers per order share one task run; a rerun of a `failed` order is keyed `<order_id>:rerun:<n>`
  and capped at 3 per order, so the public storefront route can't start unlimited runs; it is also rate-limited.
- An order left `checking` by a crash still isn't recovered (known since D4); the task's wait gives up after 90 s.
- The per-visitor limit trusts the first `X-Forwarded-For` entry, which a client may be able to forge; the global limit
  and the spend limit bound the cost regardless.
