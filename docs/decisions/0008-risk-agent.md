# 0008. Risk agent: triggers, failures, rationale fallback and personas
_Date: 2026-10-13 · Status: accepted_

## Context
D4 turns an approved order into a decision and a PayPal action. Two triggers race (the storefront's `onApprove` and the
`CHECKOUT.ORDER.APPROVED` webhook); the payment guards from ADR 0007 need the decision stored before authorising; the
model call can fail; and the storefront needs a status to poll while the detail endpoint carries payer data. D7 needs
persona data that works in a repo that goes public at D11.

## Decision
- **One function, two triggers.** `process_order` reads the PayPal order (409 unless `APPROVED` or `COMPLETED`), then
  claims the order with one conditional update to `checking`; the loser gets `202`. The webhook starts it as a FastAPI
  background task after its `200`, only when the event moved the order from `created` to `approved`.
- **Decision before money.** `decide_order` recomputes the policy from the stored receipts (`409 policy_mismatch`
  otherwise), writes the decision and `decision.made`, then runs the PayPal action through the same functions the
  `/payments` endpoints use.
- **PayPal failure after the decision** (chosen with Vic): the order goes to `failed` and the decision stays. A rerun
  reuses the stored check, decision and rationale and resumes the action; authorise and capture are idempotent.
- **Rationale fallback** (decided with Vic before the session): a 10 s timeout; on any error a template built from the
  same hit summary, marked `rationale_source: "template"` in the event.
- **Status-only endpoint** (chosen with Vic): `GET /merchant/orders/{id}/status` returns `{order_id, status}` for the
  storefront; the other read endpoints go behind D9's auth.
- **Personas** (chosen with Vic): device IDs, seeds and expected outcomes are committed in `app/personas.json` (all
  synthetic); the three sandbox buyer emails stay in `.env`. Member lists are generated from a fixed seed per member,
  so `make seed` is idempotent and the commitments stay stable.
- Tests that don't run members write a completed check directly (`tests/stored_checks.py`), replacing the D3 stand-ins.

## Alternatives considered
- Delete the decision on a PayPal failure, so `failed` always means "no decision": loses the first attempt's audit
  trail and needs a second model call.
- Storefront polls the detail endpoint through a Next.js route that strips payer data: no new endpoint, but every
  storefront poll would read receipts and payer data.
- Persona device IDs in `.env`: nothing persona-related in the repo, but D5's storefront would need the same values
  from the environment, and they aren't personal data.

## Consequences
- `failed` now has two meanings (check failed: no decision; PayPal failed: decision kept); contracts.md says so.
- A run that dies mid-way (server restart) leaves the order `checking` and its check possibly `running`; nothing
  recovers it yet.
- `/merchant/decide` and the read endpoints other than the status endpoint have no authentication yet (D9).
