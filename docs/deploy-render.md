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
| `quietcheck-report` | Static site | Free | Yes | The judges' summary, `site/index.html` (plain HTML, no build) |

About $41/month in all, from mid-October to Dec 16, 2026: the rules require the demo to stay testable until judging ends (Dec 15, 8:00 am PT; decided with Vic, 2026-10-14, extended from Nov 30). Everything is in `oregon`.

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

## Changing `render.yaml`

Validate before pushing with the Render CLI (`render login`, `render workspace set`, then
`render blueprints validate render.yaml`). It checks references and cycles, which the published JSON schema doesn't.

## Day to day

- **Deploys:** push to `main`; each service redeploys after CI passes, only if its paths changed (`buildFilter`).
  Two pushes in a row can skip a service: on 2026-10-14 the backend never deployed b99a18b because the next commit
  (web only) landed before CI finished. After quick pushes, check each service's latest deploy names the latest
  commit, and deploy it by hand if not.
- **Redeploy timing:** Render keeps the old instance serving for about a minute after the new one starts, and
  in-flight requests finish (D10 drill: 4 orders across a backend redeploy all decided). But after backend and web
  redeploy together, the web can't reach `quietcheck-backend:10000` for 1-2 minutes (seen twice). Deploy one at a
  time, and not just before a demo.
- **Env var changes** take effect on the next deploy: in the dashboard "Save and deploy", or by API
  `POST /v1/services/<id>/deploys`; the workflow needs a new version (`POST /v1/workflowversions` with
  `{"workflowId"}`).
- **Deep health:** `curl -H "Authorization: Bearer $BACKEND_API_TOKEN" <backend>/health/deep` reports the database,
  each member (reachable, URL match, list matches the expected digest) and the workflow task. After changing persona
  emails, rerun `cd backend && uv run python scripts/seed.py --digests` and update `MEMBER_DATA_HASHES` on the backend.
- **Stuck orders** are rerun automatically after 2 minutes in `checking` (feed: `reason: sweep`).
- **Smoke check / warm-up** (e.g. on recording day): `scripts/check_hosted.sh "$HOSTED_BACKEND_URL" "$HOSTED_WEB_URL"`.
  Paid instances don't sleep, so the times it prints are warm response times. With `BACKEND_API_TOKEN` in the
  environment it also checks `/health/deep`.
- **Hourly check (D10):** `.github/workflows/hosted-check.yml` runs the smoke check and the deep health check every
  hour (repo secrets `HOSTED_BACKEND_URL`, `HOSTED_WEB_URL`, `BACKEND_API_TOKEN`). A failed run emails Vic (GitHub
  notifications for failed workflows). To test the email: Actions > Hosted check > Run workflow with `force_fail`.
- **Persona run (D10, Gate C):** `make e2e-hosted` drives Ana, Ben and Cy through the shop and the PayPal pop-up and
  checks each order through the console (`ARGS="--persona ben --headed"` for one, `ARGS="--persona ana --count 5
  --together"` for the concurrency measurement). Needs the buyers' passwords and the console password in `.env`
  (`.env.example`).
- **Finding one order in the logs:** search the order ID (`ord_…`) in each service's Logs tab; backend, web and
  workflow lines all start `order <id>:` (the backend's check lines say `check <chk> for <ord>`).
- **Logs:** each service's Logs tab. Members log `serving on 0.0.0.0:<port> as http://<their hostname>:<port>,
  initiators allowed: http://quietcheck-backend-…:8100`; the backend logs `AP3 initiator serving as …; members: …`.
  The console feed shows `risk_agent.started` with `runner: render_workflow` and the task run ID for each order.
- **If AP3 checks fail with `member_unavailable`:** compare the member's logged card URL with the backend's
  `MEMBER_<M>_HOST`. They should match (both are the service's internal hostname); if not, set `MEMBER_CARD_URL` on the
  member or `MEMBER_<M>_URL` on the backend (and `INITIATOR_URL` on both sides for the initiator).
- **If orders show `runner: in_process, reason: stalled`:** Render started the workflow run but it didn't reach the
  backend within 45 s; open the run in Render (`quietcheck-risk`) for its error (often `BACKEND_HOST` or the token).
  Orders still get decided in-process.
- **If the workflow isn't used** (feed shows `runner: in_process` with no reason): check `RENDER_API_KEY` and
  `RISK_WORKFLOW_TASK` (must equal the slug on `process_order`'s task page in the dashboard) on the backend; the backend logs `workflow run not started (<error>)`. Orders still get decided in-process.
- **Keys:** `INITIATOR_PRIVATE_KEY` and `MEMBER_PRIVATE_KEY` are generated once; don't change them. Stored receipts
  keep their `verified` flag, but re-verifying an old receipt needs the initiator key that signed it.

## Shutting down (Dec 16, 2026, after judging ends)

Delete the Blueprint's services and database in the Render dashboard (or suspend them), revoke the Render API key and
the Anthropic key, delete the hosted webhook in the PayPal dashboard, and disable the hourly check
(`gh workflow disable hosted-check.yml`; it also skips itself after Dec 15).
