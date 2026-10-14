# Quiet Check: Product

_Last reviewed: 2026-10-13_

The audience, problem, demo story, personas, decision policy and out-of-scope list are in [brief.md](brief.md). This
file tracks what exists today.

## Capabilities (built)

- **AP3 private check (D2):** `POST /risk/check` checks a buyer's email, shipping address and device ID against
  Members A, B and C (9 PSI sessions, ~3 s) and returns per-member, per-field hits plus 9 verified receipts. Members run
  as separate processes and never share their lists. Idempotent per order; a failed check can be rerun.
- **PayPal payments (D3):** `GET /catalog` (7 SKUs), `POST /payments/orders` (backend-priced, intent `AUTHORIZE`),
  authorise, capture and void with status guards (money moves only from a clean decision or a reviewer on a held order),
  and `POST /webhooks/paypal` with signature verification and dedupe. Sandbox only.
- No risk agent, storefront or console yet; D4's stand-ins move order status in tests. The web app shows a placeholder page.

## Roadmap

| Deliverable | Adds |
| --- | --- |
| D2 | AP3 consortium: Members A, B, C and `POST /risk/check` with signed receipts |
| D3 | PayPal payments service: create, authorise, capture, void, webhooks |
| D4 | Merchant risk agent: approved order -> check -> policy -> PayPal action, with AI rationale |
| D5 | Shopping agent and storefront with PayPal approval |
| D6 | Merchant console: review queue, receipts, who-saw-what, review copilot |
| D7 | Demo data and the three personas |
| D8 | Dispute evidence drafts (stretch) |

## Known gaps

- AP3 is a research preview: proof fields are placeholders, and PSI shows the merchant which member matched which field.
- Sandbox only; USD only.
- Receipts are signed by the merchant (intents and result); a member's only signature is on its commitment (ADR 0004).
- Members load their lists at startup; reseeding needs a member restart.
- `/risk/check` and the `/payments` actions have no authentication yet (ADR 0006); the hosted build must keep them off the
  public surface or add auth.
- No reauthorisation endpoint: a hold older than the 3-day honour period can be reauthorised only from code (ADR 0007).
- Live payment tests need a human to approve each sandbox order once (ADR 0007).

## Non-goals

See "Out of scope" in [brief.md](brief.md).
