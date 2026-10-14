# Quiet Check: interface contracts

Source of truth (copied from the D0 contracts on 2026-10-12).
Any session that changes an interface updates this file in the same change and notes it in `STATUS.md`.

## Topology and conventions

| Process | Port (local) | Notes |
| --- | --- | --- |
| Backend (FastAPI) | 8000 | Routers: `/risk`, `/payments`, `/merchant`, `/webhooks` |
| AP3 initiator (`ap3_initiator` PrivacyAgent, in the backend process) | 8100 | Started in the backend's lifespan. It serves the merchant's AgentCard so members can verify our signed intents |
| Member A / B / C (AP3 `ap3_receiver`, A2A server) | 8101 / 8102 / 8103 | Each runs in its own process and holds one private list (about 300 hashes of all three fields). Nothing else reads it. Each answers only the initiator URLs on its allowlist (default `http://127.0.0.1:8100`; see "Member list files") |
| Web (Next.js: storefront + console) | 3000 | Its server code calls the backend at `BACKEND_URL`; the browser never does (ADR 0009). Storefront and console routes below |
| Risk workflow (Render Workflows, `workflow/main.py`) | - | Hosted only (D9, ADR 0011): one task run per approved order calls `POST /merchant/process` over the private network |

**Hosted on Render (D9, ADR 0011; `render.yaml`):** backend and web are public web services; Members A-C are private
services; Postgres and the workflow are private. All share region `oregon`. Services find each other by internal
hostname (`fromService ... host` in the Blueprint): the web and workflow reach the backend at `http://<host>:10000`,
the initiator listens on `0.0.0.0:8100` and each member on its usual port; each builds its card URL from its internal hostname (`$RENDER_DISCOVERY_SERVICE` minus `-discovery`, which Render documents as `<internal hostname>-discovery`; else `$RENDER_SERVICE_NAME`),
as `http://<host>:<port>`. A member's card URL must equal the backend's `MEMBER_<M>_URL`.

**Backend auth (D9):** when `BACKEND_API_TOKEN` is set (always on Render; the backend refuses to start there
without it), every route needs `Authorization: Bearer <token>` except `GET /health`, `POST /webhooks/paypal`
(verified by signature) and `GET /merchant/orders/{order_id}/status`. Otherwise `401 unauthorized`. The web
server and the workflow send the token; the browser never sees it. Unset locally and in tests.

- **Our IDs:** prefix + ULID: `ord_`, `chk_`, `rcp_`, `dec_`, `evt_`. Paths under `/payments/orders` and `/merchant` take
  **our** `order_id`. Authorisation and capture paths take **PayPal's** IDs.
- **Money:** `{"currency_code": "USD", "value": "49.00"}`. The value is a decimal string, as in PayPal. USD only.
- **Times:** ISO 8601 UTC, e.g. `"2026-10-20T14:03:11Z"`.
- **JSON:** snake_case, `Content-Type: application/json`.
- **Members:** `"A"`, `"B"`, `"C"`. **Fields:** `"email"`, `"shipping_address"`, `"device_id"`.

### Order status

| Status | Meaning |
| --- | --- |
| `created` | PayPal order created (intent `AUTHORIZE`), waiting for buyer approval |
| `approved` | Buyer approved in PayPal; the risk agent hasn't started |
| `checking` | Risk agent running (AP3 check, then decision) |
| `captured` | Clean (or reviewer capture): authorised and captured |
| `held` | Hold: authorised only, waiting for a reviewer |
| `blocked` | Block: never authorised; the PayPal order is left to expire |
| `voided` | A reviewer voided the held authorisation |
| `failed` | The check or a PayPal call failed. Re-run with `POST /merchant/process`. If the check failed there is no decision; if a PayPal call failed after the decision, the decision is kept and the rerun resumes its PayPal action (D4) |

Allowed transitions: `created -> approved`; `created | approved | failed -> checking` (the storefront's `onApprove` can
arrive before the webhook); `checking -> captured | held | blocked | failed`; `held -> captured | voided`.

Buyer-facing text (D5): `captured` "Paid"; `held` "Payment on hold for review"; `blocked` and `voided` "We couldn't
complete this order"; anything else "Processing". The storefront polls `GET /merchant/orders/{order_id}/status` for it.

## HTTP endpoints

### `GET /catalog`: product catalogue (D3)

The storefront and the shopping agent read the products from here. `backend/app/catalog.json` is the source of truth,
one SKU per variant; `POST /payments/orders` prices from the same file. Each item also has an optional `image`: a
path served by the web app, e.g. `"/products/backpack-blue.svg"` (D5; colour variants of one product share an image).

Response `200`:

```json
{
  "items": [
    {"sku": "backpack-blue-m", "name": "Trail Backpack, Blue, Medium", "unit_amount": {"currency_code": "USD", "value": "49.00"}, "image": "/products/backpack-blue.svg"}
  ]
}
```

The 7 SKUs: `backpack-blue-s` (45.00), `backpack-blue-m` (49.00), `backpack-blue-l` (54.00), `backpack-black-m`
(49.00), `duffel-green` (79.00), `bottle-steel` (24.00), `cap-navy` (19.00), in that order.

### `POST /payments/orders`: create order

Called by the shopping agent's `checkout` tool. The backend prices items from its own catalog and never trusts prices from
the agent. It calls PayPal `POST /v2/checkout/orders` with `intent: "AUTHORIZE"`, `custom_id` set to our `order_id`, and
`PayPal-Request-Id: <order_id>:create`.

Request:

```json
{
  "items": [{"sku": "backpack-blue-m", "quantity": 1}],
  "device_id": "dev-ana-7f3a"
}
```

Response `201`:

```json
{
  "order_id": "ord_01JA...",
  "paypal_order_id": "5O190127TN364715T",
  "status": "created",
  "amount": {"currency_code": "USD", "value": "49.00"}
}
```

The storefront passes `paypal_order_id` to the PayPal JS SDK v6 for approval. Each call creates a new order.

Validation: 1-20 items, each SKU at most once, `quantity` 1-10, `device_id` 1-128 characters; any other field (such as a
price) is `422 validation_error`. An unknown SKU is `404 not_found` with `details.sku`. The PayPal order carries the
priced `items` (category `PHYSICAL_GOODS`) and `amount.breakdown.item_total`; with no `payment_source`, PayPal returns an
`approve` link and collects the shipping address on its site (`shipping_preference` defaults to `GET_FROM_FILE`). The
order row is written only after PayPal returns, with an `order.created` event.

### `POST /payments/orders/{order_id}/authorize`: authorise

Called by `/merchant/decide` (clean and hold branches) and by the Gate A CLI. It first fetches
`GET /v2/checkout/orders/{paypal_order_id}` and requires PayPal status `APPROVED`. It then calls
`POST /v2/checkout/orders/{paypal_order_id}/authorize` with `PayPal-Request-Id: <order_id>:authorize`. This endpoint
doesn't change the order's status; `/merchant/decide` does that.

Request: `{}`

Response `200`:

```json
{
  "order_id": "ord_01JA...",
  "paypal_order_id": "5O190127TN364715T",
  "authorization_id": "0VF52814937998046",
  "authorization_status": "CREATED",
  "expires_at": "2026-11-18T14:03:11Z",
  "honor_period_ends_at": "2026-10-23T14:03:11Z"
}
```

`expires_at` comes from PayPal (holds last 29 days). `honor_period_ends_at` is the authorisation time plus 3 days.

Guards (D3): our order must be `checking` (`409 invalid_state`, `details.status`), and PayPal's `APPROVED`
(`409 invalid_state`, `details.paypal_status`). The PayPal order fetched here also fills `payer_email` and
`shipping_address` on our order if they are still empty. If PayPal's order already holds an authorisation (an earlier
call authorised but didn't store it), that authorisation is stored instead of authorising again. A repeat call after
success returns the stored authorisation. Writes a `payment.authorized` event.

Reauthorisation (after the honour period, within the 29 days) exists only as a client method in `app/paypal.py`; there
is no endpoint yet, and the sandbox can't move the clock to test it live.

### `POST /payments/authorizations/{authorization_id}/capture`: capture

Calls `POST /v2/payments/authorizations/{authorization_id}/capture` (full amount, `final_capture: true`) with
`PayPal-Request-Id: <order_id>:capture`. It is allowed only from the clean branch (`actor: "policy"`) or on a held order
(`actor: "reviewer"`).

Request:

```json
{"actor": "reviewer", "note": "Confirmed with buyer by phone"}
```

`actor` is `"policy"` or `"reviewer"`. `note` is optional and stored in `events`.

Guards (D3): `actor: "policy"` needs the order in `checking` with a stored `clean` decision; `actor: "reviewer"` needs
`held`. Otherwise `409 invalid_state` (`details.status`, `details.actor`). On success the order becomes `captured`, and a
reviewer capture also sets the decision's `resolution: "capture"`, `resolution_note` and `resolved_at`. Events:
`capture.requested` (before the PayPal call) and `payment.captured` (with `capture_id`, `capture_status`, `actor`,
`note`). A repeat call after success returns the stored result. An unknown authorisation is `404 not_found`.

Response `201`:

```json
{
  "order_id": "ord_01JA...",
  "authorization_id": "0VF52814937998046",
  "capture_id": "3C679366HH908993F",
  "capture_status": "COMPLETED",
  "amount": {"currency_code": "USD", "value": "49.00"},
  "status": "captured"
}
```

### `POST /payments/authorizations/{authorization_id}/void`: void

Calls `POST /v2/payments/authorizations/{authorization_id}/void` with `PayPal-Request-Id: <order_id>:void`. It is allowed
only on a `held` order with `actor: "reviewer"`.

Request: `{"actor": "reviewer", "note": "Buyer unreachable"}`

Any other order status or `actor: "policy"` is `409 invalid_state`. On success the order becomes `voided` and the
decision gets `resolution: "void"`. Events: `void.requested`, then `payment.voided`. A repeat call on a `voided` order
(including one voided by the `PAYMENT.AUTHORIZATION.VOIDED` webhook) returns the stored result without calling PayPal.

A PayPal failure on authorise, capture or void returns `502 paypal_error` and writes a `paypal.error` event (`action`,
`name`, `debug_id`, `issue`); the order's status is unchanged.

Response `200`:

```json
{
  "order_id": "ord_01JA...",
  "authorization_id": "0VF52814937998046",
  "authorization_status": "VOIDED",
  "status": "voided"
}
```

### `POST /risk/check`: AP3 private check

Called by the risk agent. It reads the three identifiers from the stored order, never from the caller: `payer_email`
and `shipping_address` (stored from PayPal after approval) and `device_id` (sent at order creation). It normalises and
hashes each one (see Normalisation), then runs one PSI session per identifier per member: 9 sessions, run concurrently
(about 3 s in total with ~300-entry lists). Each session stores a receipt. If any of the 9 sessions fails, the whole
check fails: no partial results and no decision.

Request:

```json
{"order_id": "ord_01JA..."}
```

The body takes `order_id` only. A body with any other field (for example `identifiers`) is rejected with
`422 validation_error`, so a caller can't check chosen values or swap clean identifiers in for a risky order. If the order
has no payer email, shipping address or device ID yet, the call returns `409 invalid_state` with `details.missing`
naming the fields, and no check is created.

The stored `shipping_address` uses the field names of PayPal's order `shipping.address` object, which the backend stores
unchanged: `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`, `country_code` (PayPal's
`address_portable`; confirmed in D3 against PayPal's Orders v2 schema and a live sandbox order).

Response `200`:

```json
{
  "check_id": "chk_01JA...",
  "order_id": "ord_01JA...",
  "hits": [
    {"member": "A", "field": "email", "is_match": false},
    {"member": "A", "field": "shipping_address", "is_match": false},
    {"member": "A", "field": "device_id", "is_match": false},
    {"member": "B", "field": "email", "is_match": true},
    {"member": "B", "field": "shipping_address", "is_match": false},
    {"member": "B", "field": "device_id", "is_match": false},
    {"member": "C", "field": "email", "is_match": false},
    {"member": "C", "field": "shipping_address", "is_match": false},
    {"member": "C", "field": "device_id", "is_match": false}
  ],
  "hit_count": 1,
  "receipts": [
    {
      "receipt_id": "rcp_01JA...",
      "member": "B",
      "field": "email",
      "session_id": "a3f1...",
      "verified": true
    }
  ]
}
```

A receipt holds what can be checked, and nothing more: the merchant's 2 signed `PrivacyIntentDirective`s for the
session (phases `init` and `msg1`, which the member verified before answering), the merchant's signed
`PrivacyResultDirective`, and the member's signed commitment from its AgentCard. `verified` is true when all of these
signatures check out, and both intents carry the result's session ID. Member replies aren't signed in AP3 1.2.2, so the
UI never calls this a "member-signed receipt".

`hits` always holds all 9 results, ordered by member and then field as above. `receipts` holds one entry per session (9
in all; only one is shown above). `hit_count` is the number of `is_match: true` entries. A repeat call for the same
`order_id` returns the stored check and doesn't rerun PSI, even if the order's identifiers have changed since.

The order must exist (`404 not_found` otherwise); this endpoint doesn't change the order's status. If the order's check
is still `running`, the call returns `409 invalid_state`. If it `failed`, the call reruns PSI on the same `check_id`,
replacing that check's receipts. On a member failure the `502 member_unavailable` details hold `check_id` and `members`
(the members whose sessions failed).

Each successful or failed check also writes a `system` event: `check.completed` (payload `check_id`, `hit_count`,
`verified`) or `check.failed` (payload `check_id`, `error`, `members`).

### `POST /merchant/start`: start the risk agent (D9)

Called by the storefront's `onApprove` (through `POST /api/orders/{order_id}/process`). Request `{"order_id"}`. An order
already `captured`, `held`, `blocked` or `voided` returns `200 {order_id, status}` and starts nothing. Otherwise it
returns `202 {order_id, status}` with the current status at once. Unless the order is `checking`, it reads the PayPal
order (storing the payer details, as `/merchant/process` does; a PayPal failure is `502 paypal_error`) and, only if
PayPal reports `APPROVED` (or `COMPLETED`), starts the risk agent, so a premature call can't use up the order's task
run: with `RENDER_API_KEY` and the workflow task set, a Render Workflows task run (`POST
https://api.render.com/v1/task-runs`, task `<RISK_WORKFLOW_SLUG>/process_order`, input `[order_id]`, idempotency key
the order ID, or `<order_id>:rerun:<n>` for a `failed` order, n = its runs so far; at most 3 reruns per order, either runner); if that call fails, or locally, in the backend process after the
response. Either way it writes a `risk_agent.started` event (payload `runner`: `render_workflow` with `task_run_id`,
or `in_process`; one event per task run). If a workflow run hasn't moved the order out of `created`/`approved` 45 s
after it started (`workflow_fallback_s`), the backend runs it in-process and writes `risk_agent.started` with
`runner: in_process, reason: stalled`. Calls for one order within 5 s of the last are answered without reading
PayPal or starting anything (`start_cooldown_s`). Unknown order: `404 not_found`. The `CHECKOUT.ORDER.APPROVED` webhook starts the risk agent the
same way, so with both triggers one task run serves the order.

The task (`process_order`) calls `/merchant/process` (subtask `run_risk_agent`, up to 3 retries on a 5xx or 401; a
404, 409 or 422 ends the run) and, on a `202`, polls the status endpoint (subtask `wait_for_decision`, up to 90 s).

### `POST /merchant/process`: run the risk agent on an approved order

Called by the risk workflow's task run (hosted) or in-process by `/merchant/start` and the `CHECKOUT.ORDER.APPROVED`
webhook handler (D9; before D9 the storefront called it directly). Two runs can race; the first claims the order. The
flow: read the order from PayPal and store its payer email and shipping address on our order, call `/risk/check`
(which uses those and the stored `device_id`), apply
the policy, have `LLM_MODEL_FAST` write the rationale, then call `/merchant/decide`.

Request: `{"order_id": "ord_01JA..."}`

Response `200` (decision made, or already made):

```json
{
  "order_id": "ord_01JA...",
  "status": "held",
  "decision": "hold",
  "decision_id": "dec_01JA...",
  "check_id": "chk_01JA..."
}
```

Response `202`, if a run for this order is already in progress: `{"order_id": "ord_01JA...", "status": "checking"}`.

Details (D4):

- An order already `captured`, `held`, `blocked` or `voided` returns the stored result (`200`) without calling PayPal,
  rerunning PSI or calling the model. A `checking` order returns `202`.
- Before claiming the order, the backend reads the PayPal order. If PayPal's status isn't `APPROVED` (or `COMPLETED`,
  when an earlier run already authorised), the call returns `409 invalid_state` with `details.paypal_status` and the
  order is unchanged.
- A failed check (`502 member_unavailable`, or `409 invalid_state` with `details.missing`) or a failed PayPal call
  (`502 paypal_error`) sets the order to `failed` and returns that error. A rerun reuses the stored check if it
  completed and the stored decision and rationale if one was made (no new model call), then resumes the PayPal action.
- The rationale call has a 10 s timeout (one retry on an empty reply, inside the same 10 s). On any model error,
  timeout or second empty reply, the decision still goes ahead with
  a template rationale built from the same hit summary, e.g. "Automatic explanation unavailable. Policy decision: hold.
  1 hit: Member B, email. Address and device were clear at all members." The `decision.made` event then has
  `rationale_source: "template"` (otherwise `"llm"`).
- The model sees only the hit summary: decision, PayPal action, hit count, which member matched which field, and which
  fields were clear everywhere. Never identifiers, hashes, list contents or other buyers' data. Prompt:
  `prompts/risk_rationale.md`.
- The webhook trigger runs only when the event moved the order from `created` to `approved`, and starts the risk agent
  as `/merchant/start` does (a task run hosted, else in the background after the `200`). Its errors are logged and
  leave the order `failed` for a rerun.

### `POST /merchant/decide`: record and execute a decision

Internal: only the risk agent calls it. The backend recomputes the policy from the stored check. If `decision` differs from
the recomputed one, the call fails with `409 policy_mismatch`, so the LLM can't change the outcome. On success the backend
writes the `decisions` row and runs that branch's PayPal action: clean authorises then captures (`actor: "policy"`); hold
authorises only; block takes no PayPal action.

Request:

```json
{
  "order_id": "ord_01JA...",
  "decision": "hold",
  "rationale": "The payer's email matched one consortium member's abuse list; address and device were clear everywhere. One signal isn't enough to decline, so funds are held for review.",
  "check_id": "chk_01JA...",
  "rationale_source": "llm"
}
```

`rationale_source` is optional (`"llm"` or `"template"`, default `"llm"`) and goes into the `decision.made` event.

Response `200`:

```json
{
  "decision_id": "dec_01JA...",
  "order_id": "ord_01JA...",
  "decision": "hold",
  "paypal_action": "authorize",
  "status": "held",
  "authorization_id": "0VF52814937998046",
  "capture_id": null
}
```

`paypal_action` is `"authorize_capture"` (clean), `"authorize"` (hold) or `"none"` (block).

Details (D4): `409 policy_mismatch` has `details.decision` and `details.policy_decision`, and writes nothing. An unknown
order, or a `check_id` that isn't this order's check, is `404 not_found`; a check that isn't `completed` is
`409 invalid_state` (`details.check_status`). A new decision needs the order in `checking` (`409 invalid_state`
otherwise). The decision row and its `decision.made` event (payload `decision_id`, `decision`, `check_id`, `hit_count`,
`paypal_action`, `rationale_source`) are written before the PayPal action, because the payment guards need the stored
decision. If the PayPal action fails, the order becomes `failed` and the decision stays; a repeat call with the same
decision resumes the action (authorise and capture return their stored results if they already happened). Otherwise
a repeat call returns the stored result.

### `POST /webhooks/paypal`: PayPal webhook receiver

Every event is first verified with `POST /v1/notifications/verify-webhook-signature`, using `PAYPAL_WEBHOOK_ID` and the
`PAYPAL-TRANSMISSION-*` headers. Each event is stored in `events` with `verified` true or false. The receiver acts only on
verified events. A duplicate PayPal event ID returns `200` without acting again.

Responses: `200 {"received": true}` for a verified event, including duplicates and event types we don't handle;
`400 webhook_unverified` if verification fails (including missing `PAYPAL-*` headers or an unset `PAYPAL_WEBHOOK_ID`);
`400 bad_request` if the body isn't a JSON event with `id` and `event_type`; `502 paypal_error` if the verification call
itself fails (nothing is stored, and PayPal redelivers).

Details (D3): bodies over 256 KB get `400 bad_request` before they are buffered or verified. The event is sent to
PayPal as the raw bytes received, not re-serialised. An unverified event is stored with `verified: false`,
`paypal_event_id` and `resource_id` null, and `payload` holding only `{claimed_id, event_type}`, so a forged delivery can't
block the real event's ID or store arbitrary content; only verified events deduplicate and act. The order is found by `resource.id` (PayPal order ID) for
`CHECKOUT.ORDER.*`, otherwise by `resource.custom_id` (our order ID) or `resource.supplementary_data.related_ids.order_id`.
Actions only follow allowed status transitions; anything else is stored only.

### Read endpoints (console and storefront)

`GET /merchant/orders` returns the newest 100 orders first (`decision` and `hit_count` are null until set):

```json
{
  "orders": [
    {
      "order_id": "ord_01JA...",
      "status": "held",
      "decision": "hold",
      "hit_count": 1,
      "amount": {"currency_code": "USD", "value": "49.00"},
      "paypal_order_id": "5O190127TN364715T",
      "authorization_id": "0VF52814937998046",
      "capture_id": null,
      "honor_period_ends_at": "2026-10-23T14:03:11Z",
      "created_at": "2026-10-20T14:01:02Z"
    }
  ]
}
```

`GET /merchant/orders/{order_id}` returns the list fields plus `items`, `device_id`, `payer_email`, `shipping_address`,
`check` (the `/risk/check` response, or null until a check completes), `receipts` (full rows, in the same order as
`check.hits`: `receipt_id`, `check_id`, `member`, `field`, `is_match`, `session_id`, `intent_directives`,
`result_directive`, `member_commitment`, `member_public_key`, `verified`, `created_at`), `decision` (the full row or
null: `decision_id`, `check_id`, `decision`, `rationale`, `rationale_source` (from the `decision.made` event),
`paypal_action`, `resolution`, `resolution_note`, `resolved_at`, `created_at`; it replaces the list's `decision`
string) and `who_saw_what`:

```json
{
  "who_saw_what": [
    {"party": "Merchant (us)", "sent": "Blinded hashes of email, address, device", "learned": "Match yes/no per member per field", "never_saw": "Any member's list"},
    {"party": "Member A", "sent": "Its PSI responses (unsigned) and its signed commitment", "learned": "Nothing about the query or result", "never_saw": "The buyer's identifiers; other members' lists"},
    {"party": "Member B", "sent": "Its PSI responses (unsigned) and its signed commitment", "learned": "Nothing about the query or result", "never_saw": "The buyer's identifiers; other members' lists"},
    {"party": "Member C", "sent": "Its PSI responses (unsigned) and its signed commitment", "learned": "Nothing about the query or result", "never_saw": "The buyer's identifiers; other members' lists"},
    {"party": "PayPal", "sent": "Payer details; payment status", "learned": "Order, authorisation, capture or void", "never_saw": "Any fraud list or check result"}
  ]
}
```

`GET /merchant/events?after=evt_...` returns `{"events": [...]}`: up to 50 `events` rows after the given ID, oldest first,
leaving out unverified webhook deliveries (stored, but anyone can post them; D9)
(from the very first event without `after`). Each has `event_id`, `source`, `event_type`, `order_id`, `paypal_event_id`,
`resource_id`, `verified`, `payload` and `created_at`. The console polls it for the live feed.

`GET /merchant/orders/{order_id}/status` returns `{"order_id": "ord_01JA...", "status": "held"}` and nothing else (D4,
decided with Vic). The storefront polls this after a `202 checking`; it carries no payer details, receipts or
rationale, so D9 can leave it public while the other read endpoints (which carry payer email, address and receipts)
go behind auth. Unknown order: `404 not_found`.

### Storefront routes (web, D5)

The browser calls only these Next.js routes (ADR 0009); they call the backend server-side.

| Route | Calls | Returns to the browser |
| --- | --- | --- |
| `POST /api/chat` | The shopping agent (`LLM_MODEL_FAST`) and its tools: `search_products` and `add_to_cart` read `GET /catalog`; `checkout` calls `POST /payments/orders` | An AI SDK UI message stream |
| `POST /api/orders/{order_id}/process` | `POST /merchant/start` (PayPal `onApprove`; D9, before that `/merchant/process`) | `{order_id, status}` (usually not final yet), or `{error: {code}}` with the backend's HTTP status |
| `GET /api/orders/{order_id}/status` | `GET /merchant/orders/{order_id}/status` | `{order_id, status}`, or `{error: {code}}` |

`/api/chat` takes `{messages, persona, cart}`: `persona` is a key from `backend/app/personas.json` (the route looks up
its `device_id`; unknown keys get `422`), and `cart` is `[{sku, quantity}]` (up to 20 lines, quantity 1-10). The
`checkout` tool's output is `{order_id, paypal_order_id, amount, items}`, or `{error}`; the page passes
`paypal_order_id` to the PayPal JS SDK v6 (`presentationMode: "popup"`), then on approval calls the process route
and polls the status route every 1.5 s, for up to 2 minutes, until `captured`, `held`, `blocked` or `voided`.

### Console routes (web, D6)

The merchant console lives at `/console` in the same app, linked from the shop header (ADR 0010). From D9, `/console`,
`/console/*` and `/api/console/*` need HTTP Basic auth with any username and the password `CONSOLE_PASSWORD`
(`web/proxy.ts`); otherwise `401` with a `WWW-Authenticate` challenge. With no `CONSOLE_PASSWORD`, `next dev` leaves
the console open and production answers `503`. The shop stays public.

**Rate limits (D9, `web/lib/limits.ts`):** `POST /api/chat`, `POST /api/console/chat` and
`POST /api/orders/{order_id}/process` each allow 20 requests per visitor (first `X-Forwarded-For` address) per 10
minutes, plus a route-wide hourly cap: 150 per hour for each chat route, 600 for the process route. Each route has its
own buckets, so a flood of public chat can't lock out the console copilot, and a lockout lasts at most an hour. In
memory on the one web instance. Over a limit: `429 {"error": {"code": "rate_limited", ...}}` with
`Retry-After`, and the chat panel says so. A model error mid-stream sends the error text `ai_unavailable: ...` when it
looks like a spent budget or provider rate limit (the panel explains), else `chat_error`. The hosted model budget is a
$10 spend limit on the Anthropic key's workspace (decided with Vic).

The browser calls only these routes. Payer email and shipping address never leave the Next.js server: the detail route
drops them, and the events route passes only these payload keys: `summary`, `decision`, `hit_count`, `verified`,
`check_id`, `rationale_source`, `paypal_action`, `actor`, `note`, `capture_id`, `capture_status`, `authorization_id`,
`authorization_status`, `action`, `name`, `debug_id`, `issue`, `error`, `runner`, `reason`, `task_run_id` (PayPal webhook
payloads hold the payer).

| Route | Calls | Returns to the browser |
| --- | --- | --- |
| `GET /api/console/orders` | `GET /merchant/orders` | `{orders}` unchanged |
| `GET /api/console/orders/{order_id}` | `GET /merchant/orders/{order_id}` | The detail without `payer_email` and `shipping_address` |
| `GET /api/console/events?after=evt_...` | `GET /merchant/events` | `{events}` with payloads cut to the keys above; a malformed `after` is `422` |
| `POST /api/console/chat` | The review copilot (`LLM_MODEL_FAST`, `prompts/review_copilot.md`) and its tools | An AI SDK UI message stream |

`/api/console/chat` takes `{messages, order_id}` (the order open in the console). Tools: `get_order` reads the detail
and gives the model a summary (status, decision, rationale, which member matched which field, receipts verified,
honour-period time left, resolution; no identifiers). `capture_held_payment` and `void_held_payment` take
`{order_id, note}`, need AI SDK tool approval (`toolApproval: 'user-approval'`, signed with
`experimental_toolApprovalSecret`), refuse any order other than the open one or one that isn't `held`, and then call
`POST /payments/authorizations/{authorization_id}/capture` or `/void` with `{"actor": "reviewer", "note"}`. Their
output is `{order_id, status, capture_id}` or `{error}`. The page polls the events route every 2 s (paging to the
newest on load) and refreshes the table and open order when any event arrives; the orders list also refreshes every
10 s.

## Webhook events handled

| Event | Action |
| --- | --- |
| `CHECKOUT.ORDER.APPROVED` | Find the order by `resource.id` (the PayPal order ID). If it's `created`, set it to `approved` and start the risk agent (D4; a Render Workflows task run when hosted, D9) |
| `PAYMENT.AUTHORIZATION.CREATED` | Record `authorization_id`, `expires_at` and `honor_period_ends_at` if missing. No status change |
| `PAYMENT.AUTHORIZATION.VOIDED` | Confirm the void: set a `held` order to `voided` |
| `PAYMENT.CAPTURE.COMPLETED` | Confirm the capture: record `capture_id` if missing and set a `checking` or `held` order to `captured` |
| `CUSTOMER.DISPUTE.CREATED` | Store only. D8 (stretch) drafts evidence with `LLM_MODEL_SMART`; a human submits it |

Confirmed live in D3 (2026-10-13, sandbox, all signature-verified): `CHECKOUT.ORDER.APPROVED`,
`PAYMENT.AUTHORIZATION.CREATED`, `PAYMENT.CAPTURE.COMPLETED` and `PAYMENT.AUTHORIZATION.VOIDED`. `CUSTOMER.DISPUTE.CREATED`
is subscribed but hasn't been seen live (D8).

## Database tables

SQLite locally, Postgres on Render. Types are given in portable terms; JSON columns are `TEXT` in SQLite and `JSONB` in Postgres.

**`orders`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | `ord_...` |
| `paypal_order_id` | text unique | |
| `status` | text | See Order status |
| `items` | json | `[{sku, name, quantity, unit_amount}]`, priced by the backend |
| `amount_value` | text | Decimal string |
| `currency_code` | text | `USD` |
| `device_id` | text | Raw, as sent by the storefront |
| `payer_email` | text null | From PayPal after approval |
| `shipping_address` | json null | From PayPal after approval |
| `authorization_id` | text null | |
| `authorization_expires_at` | timestamp null | |
| `honor_period_ends_at` | timestamp null | |
| `capture_id` | text null | |
| `created_at`, `updated_at` | timestamp | |

**`checks`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | `chk_...` |
| `order_id` | text unique FK -> orders | One check per order |
| `status` | text | `running`, `completed`, `failed` |
| `hit_count` | integer null | Set when completed |
| `error` | text null | Error code if failed: `member_unavailable`, or `internal_error` for an unexpected failure (never list contents) |
| `started_at`, `completed_at` | timestamp | |

**`receipts`**: one row per PSI session; the hits are derived from these rows.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | `rcp_...` |
| `check_id` | text FK -> checks | |
| `member` | text | `A`, `B`, `C` |
| `field` | text | `email`, `shipping_address`, `device_id` |
| `is_match` | boolean | |
| `session_id` | text | AP3 PSI session ID |
| `intent_directives` | json | Array of the 2 `PrivacyIntentDirective`s we signed, in send order (`init`, then `msg1`) |
| `result_directive` | json | `PrivacyResultDirective`, signed by the merchant |
| `member_commitment` | json | The member's commitment from its AgentCard, signed by the member |
| `member_public_key` | text | The member's Ed25519 public key (hex), from its AgentCard |
| `verified` | boolean | Intent, result and commitment signatures all verified |
| `created_at` | timestamp | |

**`decisions`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | `dec_...` |
| `order_id` | text unique FK -> orders | One decision per order |
| `check_id` | text FK -> checks | |
| `decision` | text | `clean`, `hold`, `block` |
| `rationale` | text | AI-written, 2-3 sentences; a template from the hit summary if the model failed (the `decision.made` event's `rationale_source` says which) |
| `paypal_action` | text | `authorize_capture`, `authorize`, `none` |
| `resolution` | text null | Reviewer action on holds: `capture`, `void` |
| `resolution_note` | text null | |
| `resolved_at` | timestamp null | |
| `created_at` | timestamp | |

**`events`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | `evt_...` (ULIDs sort by time) |
| `source` | text | `paypal_webhook` or `system` (our own actions: `risk_agent.started`, `check.completed`, `check.failed`, `order.created`, `payment.authorized`, `capture.requested`, `payment.captured`, `void.requested`, `payment.voided`, `paypal.error`, `decision.made`) |
| `paypal_event_id` | text unique null | Deduplicates webhook deliveries |
| `event_type` | text | e.g. `PAYMENT.CAPTURE.COMPLETED`, `decision.made` |
| `order_id` | text null FK -> orders | |
| `resource_id` | text null | PayPal resource ID |
| `verified` | boolean null | Webhooks only |
| `payload` | json | Never contains list contents |
| `created_at` | timestamp | |

## Identifier normalisation

The risk service and all three members use one shared function, so the same input always produces the same output on
every side. Normalise, then hash, before a value enters PSI or a member's list file.

| Field | Rule |
| --- | --- |
| `email` | Trim, then lowercase. No provider-specific rewriting (Gmail dots and `+tags` are kept) |
| `shipping_address` | For each of `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`: Unicode NFKC, lowercase, trim, collapse runs of whitespace to one space, remove punctuation. Also remove all spaces from `postal_code`. `country_code`: trim, uppercase ISO 3166-1 alpha-2. A missing field becomes `""`. Join the six in that order with `\|` |
| `device_id` | Trim, then lowercase |

Hash: `sha256("<field>:" + normalised_value)`, as lowercase hex, where `<field>` is `email`, `shipping_address` or
`device_id`. The prefix stops an email from matching an address. Member list files store only these hashes.

**Member list files** (D2): `<MEMBER_DATA_DIR>/<A|B|C>/list.csv`, one lowercase hex hash per line, no header, all three
fields mixed. Next to it, `key.json` holds the member's Ed25519 keypair as hex; the member creates it on first start and
keeps it. The member reads its list once at startup, refuses to start if the file is missing or empty, and publishes
`sha256` of the sorted list as its commitment's `data_hash`. The initiator's key lives in `backend/var/initiator/key.json`
so stored receipts can be re-verified after a restart. All of `backend/var/` is git-ignored. Hosted (D9), disks are wiped
on every deploy, so keys come from env vars instead: `INITIATOR_PRIVATE_KEY` and each member's `MEMBER_PRIVATE_KEY`, a
32-byte Ed25519 private key as base64 (a Render `generateValue` secret) or hex; and each member seeds its own list at
start with `scripts/seed.py --member <M>`, which needs only the emails seeded on that list (A and C: Cy's; B: Ben's) and
writes the same bytes as `make seed`.

**Initiator allowlist** (ADR 0006): a member answers only initiators whose URL (the first entry of the intent's
`participants`, compared after the SDK's `normalize_url`) is on its allowlist, set with `--initiator-url` (repeatable;
default `$INITIATOR_URL`, else `http://$INITIATOR_HOST:8100`, else `http://127.0.0.1:8100`). Anything else gets an AP3 error envelope with `error_code: "INITIATOR_NOT_ALLOWED"`,
before the member fetches any URL, and the backend reports `502 member_unavailable`.

Example: `" Ben.Buyer@Example.com "` -> `ben.buyer@example.com` -> `sha256("email:ben.buyer@example.com")`.

**Demo personas and lists** (D7): `backend/app/personas.json` holds each persona's `key`, `name`, `device_id`
(`dev-ana-7f3a`, `dev-ben-2c9d`, `dev-cy-5e1b`; synthetic), `email_env` (the `.env` variable with its sandbox buyer
email), `seeded_on` (`{member: [fields]}`) and expected `decision` and `status`. D5's persona switcher sends the
`device_id`. `scripts/seed.py` (`make seed`) writes 300 entries per member: synthetic identifiers from a fixed
per-member random seed (made-up names, `example.*` emails, made-up towns) plus the persona seeds (Ben's email on B;
Cy's email and device ID on A and C). The same emails always give byte-identical files, so it is idempotent.

D10 unit-tests these rules. List entries, normalised values and hashes never appear in logs (a CI grep enforces this).

## Environment variables

| Variable | Used by | Default / example |
| --- | --- | --- |
| `PAYPAL_CLIENT_ID` | Backend; web (JS SDK) | Sandbox REST app client ID. Public: the web server hands it to the browser |
| `BACKEND_URL` | Web (server only); workflow | `http://127.0.0.1:8000` (D5) |
| `BACKEND_HOST` | Web (server only); workflow | Hosted (D9): the backend's internal hostname from the Blueprint; used as `http://<host>:10000` when `BACKEND_URL` is unset |
| `BACKEND_API_TOKEN` | Backend; web (server only); workflow | Hosted (D9): the backend's bearer token (Blueprint `generateValue`, shared with `fromService ... envVarKey`). Unset locally: no auth |
| `CONSOLE_PASSWORD` | Web (server only) | Hosted (D9): the console's shared password (HTTP Basic, any username). Unset: console open in `next dev`, `503` in production |
| `RENDER_API_KEY` | Backend | Hosted (D9): starts the risk workflow's task runs. Unset: the risk agent runs in-process |
| `RISK_WORKFLOW_SLUG`, `RISK_WORKFLOW_TASK` | Backend | Hosted (D9): the workflow's slug (Blueprint `property: slug`; the task is `<slug>/process_order`), or the full task ID |
| `INITIATOR_PRIVATE_KEY`, `MEMBER_PRIVATE_KEY` | Backend; each member | Hosted (D9): AP3 Ed25519 keys (32 bytes, base64 or hex). Unset: the key files under `backend/var/` |
| `MEMBER_A_HOST`, `MEMBER_B_HOST`, `MEMBER_C_HOST` (or `MEMBER_<M>_URL`) | Backend | Hosted (D9): each member's internal hostname (URL `http://<host>:8101` etc.), or a full URL. Default `http://127.0.0.1:810x` |
| `INITIATOR_HOST` (or `INITIATOR_URL`) | Each member | Hosted (D9): the backend's internal hostname; the member allows only `http://<host>:8100` |
| `INITIATOR_URL`, `INITIATOR_BIND`, `INITIATOR_PORT` | Backend | The initiator's card URL (default on Render `http://<own internal hostname>:8100`, else `http://127.0.0.1:8100`), listen address (Render `0.0.0.0`, else `127.0.0.1`) and port (`8100`) |
| `MEMBER_CARD_URL` | Each member | Override for the member's card URL (default on Render `http://<own internal hostname>:<port>`) |
| `QC_TEST_POSTGRES_URL` | Backend tests | Optional: run the suite on Postgres, one fresh database per test app (CI does, D9) |
| `TOOL_APPROVAL_SECRET` | Web (server only) | Optional (D6): HMAC key that signs the copilot's approval requests. Unset: a random per-process key, fine for one local server; set it (`openssl rand -base64 32`) once the web app runs on more than one instance |
| `PAYPAL_CLIENT_SECRET` | Backend only | Sandbox REST app secret |
| `PAYPAL_WEBHOOK_ID` | Backend (webhook verification) | From the dashboard webhook config |
| `ANTHROPIC_API_KEY` | Backend; web | - |
| `LLM_PROVIDER` | Backend; web | `anthropic` |
| `LLM_MODEL_FAST` | Shopping agent, rationale, copilot | `claude-haiku-5-5` |
| `LLM_MODEL_SMART` | Dispute drafts (D8) | `claude-sonnet-5-5` |
| `DATABASE_URL` | Backend | `sqlite:///./quietcheck.db` locally; Render Postgres URL when hosted |
| `MEMBER_DATA_DIR` | Members; `scripts/seed.py` | `backend/var/members` (folders `A`, `B`, `C`; see "Member list files") |
| `PERSONA_ANA_EMAIL`, `PERSONA_BEN_EMAIL`, `PERSONA_CY_EMAIL` | `scripts/seed.py`, `scripts/live_personas.py`; hosted members (D9: A and C get Cy's, B gets Ben's) | The three sandbox buyer emails (one account each). Kept out of the repo, which goes public at D11 |

Later deliverables that add a variable list it here and in `.env.example`. Render also sets `RENDER=true`,
`RENDER_DISCOVERY_SERVICE` and `RENDER_SERVICE_NAME` on every service; the code reads them (D9).

## Errors

Every non-2xx response uses this body:

```json
{"error": {"code": "invalid_state", "message": "Order ord_01JA... is captured; void needs held.", "details": {}}}
```

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `bad_request` | Malformed JSON or headers |
| 400 | `webhook_unverified` | Webhook signature verification failed |
| 401 | `unauthorized` | Hosted: a private backend route without the right `BACKEND_API_TOKEN` (D9) |
| 404 | `not_found` | Unknown order, check, authorisation or SKU |
| 409 | `invalid_state` | The action isn't allowed in the order's current status (see transitions), or the order lacks the identifiers `/risk/check` needs |
| 409 | `policy_mismatch` | `/merchant/decide` decision differs from the policy result |
| 422 | `validation_error` | Missing, invalid or unexpected fields (e.g. `identifiers` sent to `/risk/check`); `details` names the fields |
| 429 | `rate_limited` | Web chat routes only: over the per-visitor or global limit (D9) |
| 502 | `paypal_error` | PayPal returned an error; `details` holds PayPal's `name`, `debug_id` and `issue` |
| 502 | `member_unavailable` | A consortium member didn't complete a PSI session; the check is marked `failed` |

`message` and `details` never include list contents or hashes.

## Idempotency

- **PayPal calls:** every POST to PayPal sends `PayPal-Request-Id: <order_id>:<action>`, where `<action>` is `create`,
  `authorize`, `capture` or `void`. A retry of the same action reuses the same ID, so PayPal returns the original result.
- **Our endpoints are idempotent on `order_id`.** `authorize`, `capture`, `void`, `/risk/check`, `/merchant/process` and
  `/merchant/decide` return the stored result if the action already happened for that order. They don't call PayPal or
  rerun PSI. A call that conflicts with the stored state (e.g. void after capture) returns `409 invalid_state`.
- **Webhooks:** deduplicated on `paypal_event_id`.
- **Concurrency:** `/merchant/process` moves the order to `checking` in one conditional update (`WHERE status IN
  ('approved','created','failed')`). Whichever trigger loses the race (webhook or `onApprove`) gets `202`.
- **Exception:** `POST /payments/orders` creates a new order on every call.
