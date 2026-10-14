# 0007. Payment status guards, D4 stand-ins and live sandbox tests
_Date: 2026-10-13 · Status: accepted_

## Context
D3 builds the payment endpoints before D4's `/merchant/process` and `/merchant/decide`, which own the order status
changes. The brief's guardrail says money moves only from the policy branch or on a reviewer's instruction. A PayPal order
can be approved only by a buyer in a browser, and webhooks reach the backend only through ngrok.

## Decision
- **Strict guards now** (chosen with Vic): authorise needs our order in `checking` and PayPal's in `APPROVED`; a policy
  capture needs `checking` plus a stored `clean` decision; a reviewer capture or void needs `held`. Reviewer actions fill
  `decisions.resolution`.
- Until D4 exists, tests and the Gate A CLI write the status and decision rows directly (`backend/tests/standins.py`),
  as D2's tests insert orders.
- **Approve once, saved:** `scripts/approve_orders.py` serves the backend on :8000 (so `CHECKOUT.ORDER.APPROVED` lands),
  creates orders, prints the approval links, polls PayPal until each is approved, and saves the IDs in
  `backend/var/live/approved_orders.json` (git-ignored). `pytest -m live` (skipped by default) and `scripts/gate_a.py`
  serve the same app and DB on :8000, take saved orders, and wait for verified webhooks. CI runs the same flows against
  an in-memory fake PayPal (`tests/fake_paypal.py`, an httpx `MockTransport`).
- **Webhooks:** verification sends the raw request bytes; unverified events are stored with `paypal_event_id` null.
- **Reauthorise:** a client method only, no endpoint (chosen with Vic).

## Alternatives considered
- Looser guards until D4: less setup, but D4 would have to tighten them, and the guardrail would be untested meanwhile.
- A saved PayPal wallet (vault) to skip approval: documented only with `CAPTURE`, may need reference transactions, and
  skips the demo's approval step (ruled out before D3).
- Live tests against `make dev-backend`: simpler code, but the tests couldn't read the DB directly or control its file.
- Playwright to automate approval: revisit in D10.

## Consequences
- D4's `/merchant/decide` must write the decision row before calling authorise and capture, and set `held` after
  authorising a hold; `/merchant/process` must move the order to `checking` and call `store_payer_details` first.
- Live runs need `make dev-backend` stopped, `ngrok http 8000` running, and saved orders used soon after approval (one
  third-party guide says 3 hours; PayPal's docs don't confirm a figure).
