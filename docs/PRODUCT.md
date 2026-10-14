# Quiet Check: Product

_Last reviewed: 2026-10-14_

The audience, problem, demo story, personas, decision policy and out-of-scope list are in [brief.md](brief.md). This
file tracks what exists today.

## Capabilities (built)

- **AP3 private check (D2):** `POST /risk/check` checks a buyer's email, shipping address and device ID against
  Members A, B and C (9 PSI sessions, ~3 s) and returns per-member, per-field hits plus 9 verified receipts. Members run
  as separate processes and never share their lists. Idempotent per order; a failed check can be rerun.
- **PayPal payments (D3):** `GET /catalog` (7 SKUs), `POST /payments/orders` (backend-priced, intent `AUTHORIZE`),
  authorise, capture and void with status guards (money moves only from a clean decision or a reviewer on a held order),
  and `POST /webhooks/paypal` with signature verification and dedupe. Sandbox only.
- **Risk agent (D4):** `POST /merchant/process` takes an approved order (from the `CHECKOUT.ORDER.APPROVED` webhook or
  the storefront, whichever comes first) through the check, the fixed policy, a 2-3 sentence AI rationale and the
  PayPal action: clean is captured, a hold is authorised and waits for a reviewer, a block is never authorised. The
  model only explains: `/merchant/decide` rejects any decision the policy didn't make, and a template rationale stands
  in if the model fails. Read endpoints list orders, show one order with its receipts, decision and who-saw-what, give
  the storefront a status-only view, and feed the console's event log.
- **Demo data (D7):** `make seed` writes three ~300-entry synthetic member lists with the personas seeded: Ana (clean),
  Ben (one email hit at Member B: hold), Cy (email and device at Members A and C: block).
- **Storefront and shopping agent (D5):** a seven-product shop with a chat panel. Ask the agent ("buy the blue backpack
  in medium") and it searches, adds to the cart and creates the PayPal order in one turn (about 5 s), then shows a
  PayPal button; the buyer approves in the PayPal pop-up and sees "Paid", "Payment on hold for review" or "We couldn't
  complete this order". Products can also be added by hand and checked out through the agent. A persona switcher
  picks Ana, Ben or Cy (their device ID); the buyer logs in to PayPal as that persona's sandbox account.
- **Merchant console and review copilot (D6):** `/console`, linked from the shop header. An orders table (status,
  decision, hits, PayPal order, authorisation and capture IDs, time left in the 3-day honour period for held orders);
  the open order's AI rationale, a 3 x 3 member-by-field hit grid, its 9 receipts (verified, with the signed intents,
  result and member commitment) and the who-saw-what panel; a live feed of PayPal webhooks and our own events. The
  copilot explains a hold ("why is this on hold?") and, on the reviewer's instruction, captures or voids the held
  payment after one Approve on a card showing the order, amount, action and note. Hosted, the console asks for a shared password (D9).

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
- An order whose risk-agent run dies mid-way (for example, a server restart) stays `checking`; there is no sweeper yet.

## Non-goals

See "Out of scope" in [brief.md](brief.md).
