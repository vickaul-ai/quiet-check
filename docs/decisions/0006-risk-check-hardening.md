# 0006. Risk check reads identifiers from the order; members allowlist initiators
_Date: 2026-10-13 · Status: accepted_

## Context
An automated security review of the D2 commit found three issues:
- `POST /risk/check` took the identifiers from the request body. Anyone who could reach the backend with an existing
  order ID could ask whether chosen values are on the members' lists, and a caller could check clean identifiers for a
  risky order.
- `/risk/check` has no authentication (contracts.md defines none).
- Members set `allow_private_initiator_urls=True` (ADR 0004), which turns off the AP3 SDK's guard against forged
  initiator URLs. The initiator URL comes from the unauthenticated envelope, and the SDK fetches its AgentCard before
  checking any signature, so anyone who can reach a member could make it request private or cloud-metadata URLs.

## Decision
- `/risk/check` takes `{"order_id"}` only and reads `payer_email`, `shipping_address` and `device_id` from the order
  row. Extra body fields are rejected (422). An order missing any of them returns `409 invalid_state`.
- Each member runs as a `MemberAgent`, a `PrivacyAgent` subclass that refuses an envelope unless its intent's initiator
  URL is on the member's allowlist (`--initiator-url`, default `http://127.0.0.1:8100`). The check runs before the SDK
  sees the envelope, so no unlisted URL is ever fetched. `allow_private_initiator_urls=True` stays, because our own
  initiator is on localhost (and a private network on Render).

## Alternatives considered
- Shared-secret or token auth on `/risk/check`: still worth doing for the hosted build, but it doesn't stop an
  authorised caller from choosing values; reading from the order does.
- Turning the SDK guard back on: it blocks loopback and private addresses, which is exactly where our initiator runs.

## Consequences
- D4's `/merchant/process` must store the payer email and shipping address on the order before calling `/risk/check`.
- Not fixed: `/risk/check` still has no authentication. Anyone who can reach the backend can trigger the one-time check
  for an existing order. The email and address come from PayPal, but the device ID is whatever the storefront sent at
  order creation, so someone who creates and approves a sandbox order could still test one chosen device ID per order.
  D9 should keep `/risk/check` (and `/merchant/decide`) off the public surface or put auth on them.
- D9 must pass the initiator's Render URL to each member with `--initiator-url`.
