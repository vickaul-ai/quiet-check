# Deploying to Render

The hosted demo runs on Render from `render.yaml` (a Blueprint). ADR 0011 explains the design, `docs/contracts.md` lists
the variables.

## What gets created

| Resource | Type | Plan | Public | Notes |
| --- | --- | --- | --- | --- |
| `quietcheck-db` | Postgres 18 | basic-256mb ($6/mo) | No | Private network only |
| `quietcheck-backend` | Web service (Python) | Starter ($7/mo) | Yes | Only `/health`, the PayPal webhook and the order status are open; the rest needs `BACKEND_API_TOKEN`. AP3 initiator on private port 8100 |
| `quietcheck-web` | Web service (Node) | Starter ($7/mo) | Yes | Shop public; `/console` behind `CONSOLE_PASSWORD` |
| `quietcheck-member-a`, `-b`, `-c` | Private services (Python) | Starter ($7/mo each) | No | Seed their own list at start; answer only the backend's initiator |
| `quietcheck-risk` | Workflow (Python) | Per second of task runs | No | One `process_order` run per approved order |

About $41/month in all, from mid-October to Nov 30, 2026 (decided with Vic). Everything is in `oregon`.

## First deploy

Run the wizard from the repo root; it opens each page, says what to click, and puts values from your `.env` on the
clipboard:

```sh
bash scripts/render_setup.sh
```

Its stages, for reference:

1. CI on `main` is green (services deploy only after checks pass).
2. Anthropic: a key in a workspace with a $10 spend limit.
3. Render: an API key (`RENDER_API_KEY`) so the backend can start workflow runs.
4. Render: New Blueprint Instance from `vickaul-ai/quiet-check`, filling in the `sync: false` values: PayPal client
   ID (backend and web), client secret, `PAYPAL_WEBHOOK_ID` (`pending` for now), the Anthropic key (backend and web),
   `RENDER_API_KEY`, `CONSOLE_PASSWORD`, and the persona emails (Cy's on A and C, Ben's on B).
5. Wait until everything is Live; note the backend and web URLs.
6. PayPal: add a second webhook to the same sandbox app at `<backend URL>/webhooks/paypal` with the 5 events, then set
   its ID as the backend's `PAYPAL_WEBHOOK_ID` and redeploy. The ngrok webhook stays for local runs.
7. `scripts/check_hosted.sh <backend URL> <web URL>`: public routes answer, private ones refuse.
8. Run Ana, Ben and Cy on the hosted shop (log out at www.sandbox.paypal.com between buyers).

## Day to day

- **Deploys:** push to `main`; each service redeploys after CI passes, only if its paths changed (`buildFilter`).
- **Smoke check / warm-up** (e.g. on recording day): `scripts/check_hosted.sh "$HOSTED_BACKEND_URL" "$HOSTED_WEB_URL"`.
  Paid instances don't sleep, so the times it prints are warm response times.
- **Logs:** each service's Logs tab. Members log `initiators allowed: http://quietcheck-backend-…:8100` at start; the
  console feed shows `risk_agent.started` with `runner: render_workflow` and the task run ID for each order.
- **If AP3 checks fail with `member_unavailable`:** compare the member's logged card URL with the backend's
  `MEMBER_<M>_HOST`. They should match (both are the service's internal hostname); if not, set `MEMBER_CARD_URL` on the
  member or `MEMBER_<M>_URL` on the backend (and `INITIATOR_URL` on both sides for the initiator).
- **If the workflow isn't used** (feed shows `runner: in_process`): check `RENDER_API_KEY` and `RISK_WORKFLOW_SLUG` on
  the backend; the backend logs `workflow run not started (<error>)`. Orders still get decided in-process.
- **Keys:** `INITIATOR_PRIVATE_KEY` and `MEMBER_PRIVATE_KEY` are generated once; don't change them. Stored receipts
  keep their `verified` flag, but re-verifying an old receipt needs the initiator key that signed it.

## Shutting down (Nov 30, 2026)

Delete the Blueprint's services and database in the Render dashboard (or suspend them), revoke the Render API key and
the Anthropic key, and delete the hosted webhook in the PayPal dashboard.
