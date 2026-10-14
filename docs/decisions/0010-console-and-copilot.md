# 0010. Merchant console and review copilot: our endpoints, signed tool approval, no payer details in the browser
_Date: 2026-10-14 · Status: accepted_

## Context
D6 adds the merchant console and a review copilot that explains a hold and captures or voids the held payment on the
reviewer's instruction. The spec first named `paypal-agent-toolkit` for the copilot's actions. The console has no login
until D9, and the backend's read endpoints carry payer email, shipping address and full PayPal webhook payloads.

## Decision
- **No PayPal Agent Toolkit** (decided with Vic, 2026-10-14). The copilot's money tools call our
  `POST /payments/authorizations/{id}/capture` and `/void` with `actor: "reviewer"`, so there is one money path with
  the `held` guard, the decision's `resolution` fields and the events. The toolkit also has no void.
- **Console at `/console`** in the web app, linked from the shop header, no gate until D9 (decided with Vic). Its reads
  go through Next.js server routes like D5's (ADR 0009).
- **One confirmation = AI SDK v7 tool approval** (decided with Vic): `toolApproval: 'user-approval'` on
  `capture_held_payment` and `void_held_payment`. The card shows order, amount, action and note with Approve and Deny;
  the tool runs only on the request that carries the Approve. Approvals are HMAC-signed with
  `experimental_toolApprovalSecret` (`TOOL_APPROVAL_SECRET`, or a random per-process key locally), so a client can't
  fabricate an Approve; a forged one fails with `AI_InvalidToolApprovalSignatureError` before any tool runs.
- **The copilot acts only on the open order.** The chat is keyed on the order open in the console; the money tools refuse
  any other order ID and any order that isn't `held` (the backend checks `held` again).
- **Payer details stay on the server.** The console needs no payer email or address, so the detail route drops them and
  the events route passes only a short list of payload keys. The model's order summary carries no identifiers either.
- **Polling, not push.** The page polls the events route every 2 s and refreshes on any new event. That is enough for
  one reviewer on localhost and needs no new backend endpoint.
- **Model:** `LLM_MODEL_FAST` (Haiku 5.5). Its answers were accurate in testing, so the Sonnet switch in
  `web/lib/model.ts` wasn't needed.

## Alternatives considered
- PayPal Agent Toolkit for capture: bypasses our status guards and events, and has no void.
- Server-sent events from the backend: lower latency, but a new endpoint and connection handling for no demo benefit.
- A free-form copilot across all orders: more flexible, but a wrong order ID is easier to approve by mistake.

## Consequences
- D9 must put `/console` and `/api/console/*` behind auth (they expose order IDs, receipts and the money actions), set
  `TOOL_APPROVAL_SECRET` if the web service runs more than one instance, and rate-limit `/api/console/chat`.
- A reload clears the copilot chat; a pending approval card is lost, and nothing was done.
