# Quiet Check: interface contracts

Source of truth (copied from the D0 contracts on 2026-10-12).
Any session that changes an interface updates this file in the same change and notes it in `STATUS.md`.

## Topology and conventions

| Process | Port (local) | Notes |
| --- | --- | --- |
| Backend (FastAPI) | 8000 | Routers: `/risk`, `/payments`, `/merchant`, `/webhooks` |
| AP3 initiator (`ap3_initiator` PrivacyAgent, in the backend process) | 8100 | Started in the backend's lifespan. It serves the merchant's AgentCard so members can verify our signed intents |
| Member A / B / C (AP3 `ap3_receiver`, A2A server) | 8101 / 8102 / 8103 | Each runs in its own process and holds one private list (about 300 hashes of all three fields). Nothing else reads it. Each answers only the initiator URLs on its allowlist (default `http://127.0.0.1:8100`; see "Member list files") |
| Web (Next.js: storefront + console) | 3000 | Calls the backend only |

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
| `failed` | The check or a PayPal call failed; no decision recorded. Re-run with `POST /merchant/process` |

Allowed transitions: `created -> approved`; `created | approved | failed -> checking` (the storefront's `onApprove` can
arrive before the webhook); `checking -> captured | held | blocked | failed`; `held -> captured | voided`.

Buyer-facing text (D5): `captured` "Paid"; `held` "Payment on hold for review"; `blocked` and `voided` "We couldn't
complete this order"; anything else "Processing".

## HTTP endpoints

### `GET /catalog`: product catalogue (D3)

The storefront and the shopping agent read the products from here. `backend/app/catalog.json` is the source of truth,
one SKU per variant; `POST /payments/orders` prices from the same file. D5 adds images (new optional fields only).

Response `200`:

```json
{
  "items": [
    {"sku": "backpack-blue-m", "name": "Trail Backpack, Blue, Medium", "unit_amount": {"currency_code": "USD", "value": "49.00"}}
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

### `POST /merchant/process`: run the risk agent on an approved order

Called by the storefront's `onApprove` and by the `CHECKOUT.ORDER.APPROVED` webhook handler, whichever comes first. The
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
  "check_id": "chk_01JA..."
}
```

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

`GET /merchant/orders` returns the newest orders first:

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
`check` (the `/risk/check` response), `receipts` (full rows, including the signed intent and result directives),
`decision` (the full row, including the rationale) and `who_saw_what`:

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

`GET /merchant/events?after=evt_...` returns `{"events": [...]}`: up to 50 `events` rows after the given ID, oldest first.
The console polls it for the live feed.

## Webhook events handled

| Event | Action |
| --- | --- |
| `CHECKOUT.ORDER.APPROVED` | Find the order by `resource.id` (the PayPal order ID). If it's `created`, set it to `approved` and run `/merchant/process` (the trigger arrives with D4) |
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
| `rationale` | text | AI-written, 2-3 sentences |
| `paypal_action` | text | `authorize_capture`, `authorize`, `none` |
| `resolution` | text null | Reviewer action on holds: `capture`, `void` |
| `resolution_note` | text null | |
| `resolved_at` | timestamp null | |
| `created_at` | timestamp | |

**`events`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | `evt_...` (ULIDs sort by time) |
| `source` | text | `paypal_webhook` or `system` (our own actions: `check.completed`, `check.failed`, `order.created`, `payment.authorized`, `capture.requested`, `payment.captured`, `void.requested`, `payment.voided`, `paypal.error`; D4 adds `decision.made`) |
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
so stored receipts can be re-verified after a restart. All of `backend/var/` is git-ignored.

**Initiator allowlist** (ADR 0006): a member answers only initiators whose URL (the first entry of the intent's
`participants`, compared after the SDK's `normalize_url`) is on its allowlist, set with `--initiator-url` (repeatable;
default `http://127.0.0.1:8100`). Anything else gets an AP3 error envelope with `error_code: "INITIATOR_NOT_ALLOWED"`,
before the member fetches any URL, and the backend reports `502 member_unavailable`.

Example: `" Ben.Buyer@Example.com "` -> `ben.buyer@example.com` -> `sha256("email:ben.buyer@example.com")`.

D10 unit-tests these rules. List entries, normalised values and hashes never appear in logs (a CI grep enforces this).

## Environment variables

| Variable | Used by | Default / example |
| --- | --- | --- |
| `PAYPAL_CLIENT_ID` | Backend; web (JS SDK) | Sandbox REST app client ID |
| `PAYPAL_CLIENT_SECRET` | Backend only | Sandbox REST app secret |
| `PAYPAL_WEBHOOK_ID` | Backend (webhook verification) | From the dashboard webhook config |
| `ANTHROPIC_API_KEY` | Backend; web | - |
| `LLM_PROVIDER` | Backend; web | `anthropic` |
| `LLM_MODEL_FAST` | Shopping agent, rationale, copilot | `claude-haiku-5-5` |
| `LLM_MODEL_SMART` | Dispute drafts (D8) | `claude-sonnet-5-5` |
| `DATABASE_URL` | Backend | `sqlite:///./quietcheck.db` locally; Render Postgres URL when hosted |

| `MEMBER_DATA_DIR` | Members | `backend/var/members` (folders `A`, `B`, `C`; see "Member list files") |

Later deliverables that add a variable (for example, member URLs for Render in D9) list it here and in `.env.example`.

## Errors

Every non-2xx response uses this body:

```json
{"error": {"code": "invalid_state", "message": "Order ord_01JA... is captured; void needs held.", "details": {}}}
```

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `bad_request` | Malformed JSON or headers |
| 400 | `webhook_unverified` | Webhook signature verification failed |
| 404 | `not_found` | Unknown order, check, authorisation or SKU |
| 409 | `invalid_state` | The action isn't allowed in the order's current status (see transitions), or the order lacks the identifiers `/risk/check` needs |
| 409 | `policy_mismatch` | `/merchant/decide` decision differs from the policy result |
| 422 | `validation_error` | Missing, invalid or unexpected fields (e.g. `identifiers` sent to `/risk/check`); `details` names the fields |
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
