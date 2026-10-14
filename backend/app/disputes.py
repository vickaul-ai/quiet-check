"""Dispute evidence drafts (D8; docs/contracts.md, ADR 0012).

A verified `CUSTOMER.DISPUTE.*` webhook starts `sync_dispute` after its 200: read the dispute from
PayPal (a webhook resource can be partial), match it to our order (the disputed transaction's
`seller_transaction_id` is our capture ID; else its `custom` is our order ID, which we send as
`custom_id`), store it in `disputes`, and draft an evidence note once per dispute.

`LLM_MODEL_SMART` writes the draft from a fact sheet with no payer email, address, device ID,
hashes or list contents: the dispute, the order and its timeline, the policy decision and
rationale, which member matched which field, the receipt and AP3 session IDs, and which PayPal
webhooks were signature-verified. Any model error, timeout or empty reply falls back to a template
built from the same facts. The model only writes text: a reviewer edits the note and submits it
(`submit_evidence`), and nothing submits on its own.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, Engine, and_, desc, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import MEMBERS, REPO_DIR, Settings
from app.db import checks, decisions, disputes, events, orders, receipts, utcnow
from app.errors import APIError
from app.identifiers import FIELDS
from app.payments import add_event, iso_z, money, paypal_error
from app.paypal import PayPalClient, PayPalError
from app.policy import PAYPAL_ACTIONS

logger = logging.getLogger("quietcheck.disputes")

PROMPT_PATH = REPO_DIR / "prompts" / "dispute_evidence.md"
DISPUTE_EVENTS = (
    "CUSTOMER.DISPUTE.CREATED",
    "CUSTOMER.DISPUTE.UPDATED",
    "CUSTOMER.DISPUTE.RESOLVED",
)
AWAITING_SELLER = "WAITING_FOR_SELLER_RESPONSE"
MAX_NOTE = 2000  # PayPal's limit on evidence notes
# A draft claimed longer ago than this (the process died mid-draft) may be claimed again.
STALE_DRAFT = timedelta(minutes=5)

REASON_TEXT = {
    "UNAUTHORISED": "the buyer says they did not authorise this payment",
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "the buyer says the item was not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "the buyer says the item was not as described",
    "CREDIT_NOT_PROCESSED": "the buyer says a refund or credit was not processed",
    "DUPLICATE_TRANSACTION": "the buyer says they were charged twice",
    "INCORRECT_AMOUNT": "the buyer says the amount charged was wrong",
    "PAYMENT_BY_OTHER_MEANS": "the buyer says they paid another way",
    "CANCELED_RECURRING_BILLING": "the buyer says a cancelled recurring payment was charged",
    "PROBLEM_WITH_REMITTANCE": "the buyer reports a problem with the remittance",
}
ACTION_TEXT = {
    "authorize_capture": "authorise, then capture",
    "authorize": "authorise and hold for a reviewer",
    "none": "do not authorise",
}
FIELD_TEXT = {"email": "email", "shipping_address": "shipping address", "device_id": "device ID"}

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def safe_id(value: object) -> str:
    """Dispute IDs come from PayPal and the URL: log only well-formed ones."""
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else "(invalid id)"


def _parse_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


# Matching a dispute to our order


def order_conditions(resource: dict) -> list:
    """Ways a dispute names our order: the capture ID PayPal calls `seller_transaction_id`,
    or `custom` (the `custom_id` we set to our order ID at checkout)."""
    conditions = []
    for txn in resource.get("disputed_transactions") or []:
        if not isinstance(txn, dict):
            continue
        if txn.get("seller_transaction_id"):
            conditions.append(orders.c.capture_id == str(txn["seller_transaction_id"]))
        if txn.get("custom"):
            conditions.append(orders.c.id == str(txn["custom"]))
    return conditions


def match_order(conn: Connection, resource: dict) -> str | None:
    """Our order for a dispute, preferring the capture ID over `custom`."""
    for condition in order_conditions(resource):
        row = conn.execute(select(orders.c.id).where(condition)).first()
        if row:
            return row.id
    return None


def can_provide_evidence(dispute: dict) -> bool:
    """PayPal shows a provide-evidence HATEOAS link only when it will take evidence."""
    for link in dispute.get("links") or []:
        rel = str(link.get("rel", "")).replace("-", "_")
        if rel == "provide_evidence" or str(link.get("href", "")).endswith("/provide-evidence"):
            return True
    return False


# Storing


def _dispute_values(dispute: dict) -> dict:
    amount = dispute.get("dispute_amount") or {}
    values = {
        "reason": dispute.get("reason"),
        "status": dispute.get("status"),
        "amount_value": amount.get("value"),
        "currency_code": amount.get("currency_code"),
        "seller_response_due_date": _parse_time(dispute.get("seller_response_due_date")),
    }
    return {k: v for k, v in values.items() if v is not None}


def upsert_dispute(engine: Engine, dispute_id: str, dispute: dict, order_id: str | None) -> None:
    values = _dispute_values(dispute)
    with engine.begin() as conn:
        try:
            with conn.begin_nested():
                conn.execute(insert(disputes).values(id=dispute_id, order_id=order_id, **values))
        except IntegrityError:
            if values:
                conn.execute(update(disputes).where(disputes.c.id == dispute_id).values(values))
            if order_id:
                conn.execute(
                    update(disputes)
                    .where(disputes.c.id == dispute_id, disputes.c.order_id.is_(None))
                    .values(order_id=order_id)
                )


def load_dispute(conn: Connection, dispute_id: str):
    row = conn.execute(select(disputes).where(disputes.c.id == dispute_id)).first()
    if row is None:
        raise APIError(404, "not_found", "Dispute not found.")
    return row


def dispute_body(row) -> dict:
    """What the console sees: no payer data (the disputes table holds none)."""
    pending = row.draft_source == "pending"
    return {
        "dispute_id": row.id,
        "order_id": row.order_id,
        "reason": row.reason,
        "status": row.status,
        "amount": money(row.amount_value, row.currency_code or "USD") if row.amount_value else None,
        "seller_response_due_date": iso_z(row.seller_response_due_date),
        "draft_note": None if pending else row.draft_note,
        "draft_source": row.draft_source,
        "drafted_at": iso_z(row.drafted_at),
        "submitted_note": row.submitted_note,
        "submitted_at": iso_z(row.submitted_at),
        "submitted_by": row.submitted_by,
        "created_at": iso_z(row.created_at),
        "updated_at": iso_z(row.updated_at),
    }


def order_disputes(conn: Connection, order_id: str) -> list[dict]:
    rows = conn.execute(
        select(disputes)
        .where(disputes.c.order_id == order_id)
        .order_by(desc(disputes.c.created_at), desc(disputes.c.id))
    ).all()
    return [dispute_body(r) for r in rows]


def latest_status_column():
    """The newest dispute's PayPal status for each order (the orders list's `dispute_status`)."""
    return (
        select(disputes.c.status)
        .where(disputes.c.order_id == orders.c.id)
        .order_by(desc(disputes.c.created_at), desc(disputes.c.id))
        .limit(1)
        .correlate(orders)
        .scalar_subquery()
        .label("dispute_status")
    )


# The fact sheet the model sees


def _event_time(rows, event_type: str, webhook: bool = False) -> str | None:
    for row in rows:
        if row.event_type == event_type and (not webhook or row.verified):
            return iso_z(row.created_at)
    return None


def fact_sheet(conn: Connection, dispute_row) -> dict:
    """Everything the draft may say. Deliberately left out: payer email, shipping address,
    device ID, buyer names, identifier hashes, list contents and reviewer notes (free text)."""
    order = conn.execute(select(orders).where(orders.c.id == dispute_row.order_id)).first()
    check = conn.execute(select(checks).where(checks.c.order_id == order.id)).first()
    decision = conn.execute(select(decisions).where(decisions.c.order_id == order.id)).first()
    receipt_rows = (
        conn.execute(
            select(receipts).where(receipts.c.check_id == check.id).order_by(receipts.c.id)
        ).all()
        if check
        else []
    )
    event_rows = conn.execute(
        select(events).where(events.c.order_id == order.id).order_by(events.c.id)
    ).all()
    made = next((r.payload for r in event_rows if r.event_type == "decision.made"), {})
    captured = next((r.payload for r in event_rows if r.event_type == "payment.captured"), {})

    matched = [
        {"member": f"Member {r.member}", "field": FIELD_TEXT[r.field]}
        for r in receipt_rows
        if r.is_match
    ]
    hit_fields = {r.field for r in receipt_rows if r.is_match}
    webhooks = [
        {"event_type": r.event_type, "received_at": iso_z(r.created_at), "signature_verified": True}
        for r in event_rows
        if r.source == "paypal_webhook" and r.verified and r.event_type not in DISPUTE_EVENTS
    ]
    return {
        "dispute": {
            "dispute_id": dispute_row.id,
            "reason": dispute_row.reason,
            "reason_in_words": REASON_TEXT.get(dispute_row.reason or "", "other"),
            "status": dispute_row.status,
            "amount": f"{dispute_row.amount_value} {dispute_row.currency_code}"
            if dispute_row.amount_value
            else None,
            "seller_response_due_date": iso_z(dispute_row.seller_response_due_date),
        },
        "order": {
            "order_id": order.id,
            "items": [f"{i['quantity']} x {i['name']}" for i in order.items],
            "amount": f"{order.amount_value} {order.currency_code}",
            "status": order.status,
            "created_at": iso_z(order.created_at),
            # The risk agent starts only once PayPal reports the order APPROVED (buyer login
            # and approval happen on PayPal's site), so a decision implies approval.
            "buyer_approved_in_paypal": decision is not None,
            "approved_webhook_at": _event_time(event_rows, "CHECKOUT.ORDER.APPROVED", True),
            "checked_at": iso_z(check.completed_at) if check else None,
            "decided_at": iso_z(decision.created_at) if decision else None,
            "authorized_at": _event_time(event_rows, "payment.authorized"),
            "captured_at": _event_time(event_rows, "payment.captured"),
            "captured_by": captured.get("actor"),  # "policy" (clean) or "reviewer" (after a hold)
        },
        "paypal_ids": {
            "paypal_order_id": order.paypal_order_id,
            "authorization_id": order.authorization_id,
            "capture_id": order.capture_id,
        },
        "decision": {
            "decision": decision.decision,
            "paypal_action": PAYPAL_ACTIONS[decision.decision],
            "rationale": decision.rationale,
            "rationale_source": made.get("rationale_source"),
            "reviewer_resolution": decision.resolution,
        }
        if decision
        else None,
        "consortium_check": {
            "method": "AP3 private set intersection, one session per member per field",
            "members_checked": [f"Member {m}" for m in MEMBERS],
            "fields_checked": [FIELD_TEXT[f] for f in FIELDS],
            "hit_count": check.hit_count if check else None,
            "hits": matched,
            "fields_clear_at_all_members": [FIELD_TEXT[f] for f in FIELDS if f not in hit_fields],
        }
        if check and check.status == "completed"
        else None,
        "receipts": {
            "count": len(receipt_rows),
            "verified": sum(1 for r in receipt_rows if r.verified),
            "items": [
                {
                    "receipt_id": r.id,
                    "member": f"Member {r.member}",
                    "field": FIELD_TEXT[r.field],
                    "ap3_session_id": r.session_id,
                    "verified": r.verified,
                }
                for r in receipt_rows
            ],
        },
        "paypal_webhooks_verified": webhooks,
    }


# Drafting


def _join(words: list[str]) -> str:
    return " and ".join(words) if len(words) <= 2 else ", ".join(words[:-1]) + " and " + words[-1]


REASON_LINES = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": (
        "This note covers the payment record only; shipping and tracking evidence is not part "
        "of it and will be provided separately."
    ),
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": (
        "This note covers the payment record only; the item description and its condition are "
        "not part of it."
    ),
    "DUPLICATE_TRANSACTION": (
        "This order has one authorisation and one capture; there is no second charge for it."
    ),
    "INCORRECT_AMOUNT": (
        "The amount captured equals the order total, priced by our server from our catalogue."
    ),
}


def template_note(facts: dict) -> str:
    """The fallback draft: the same facts in fixed sentences, at most MAX_NOTE characters."""
    d, o, ids = facts["dispute"], facts["order"], facts["paypal_ids"]
    dec, chk, rcp = facts["decision"], facts["consortium_check"], facts["receipts"]
    parts = [
        f"Response to PayPal dispute {d['dispute_id']}: {d['reason_in_words']}.",
        f"Order {o['order_id']} (PayPal order {ids['paypal_order_id']}): {', '.join(o['items'])}, "
        f"total {o['amount']}, created {o['created_at']}.",
    ]
    if ids["authorization_id"]:
        line = f"Authorised {o['authorized_at'] or ''} as authorisation {ids['authorization_id']}"
        if ids["capture_id"]:
            by = " after a reviewer's check" if o["captured_by"] == "reviewer" else ""
            line += f"; captured {o['captured_at'] or ''}{by} as capture {ids['capture_id']}"
        parts.append(" ".join(line.split()) + ".")
    if chk:
        if chk["hits"]:
            found = "; ".join(f"{h['member']} matched the {h['field']}" for h in chk["hits"])
        else:
            found = "no member matched any of them"
        parts.append(
            f"Before authorising, we checked the buyer's {_join(chk['fields_checked'])} privately "
            f"against the abuse lists of 3 other merchants ({_join(chk['members_checked'])}) with "
            f"AP3 private set intersection: {found}."
        )
    if dec:
        resolution = dec["reviewer_resolution"]
        resolved = f" A reviewer then chose to {resolution} the held payment." if resolution else ""
        action = ACTION_TEXT[dec["paypal_action"]]
        parts.append(f"Our fixed policy decided {dec['decision']} ({action}).{resolved}")
    if facts["paypal_webhooks_verified"]:
        kinds = _join(sorted({w["event_type"] for w in facts["paypal_webhooks_verified"]}))
        parts.append(f"PayPal's webhooks for this order ({kinds}) were signature-verified.")
    if d["reason"] == "UNAUTHORISED" and o["buyer_approved_in_paypal"]:
        parts.append(
            "PayPal reported the order approved by the buyer in PayPal's own checkout before we "
            "authorised it."
        )
    parts.append(REASON_LINES.get(d["reason"] or "", ""))
    if chk:
        listed = (
            "the buyer's identifiers were not on those merchants' abuse lists"
            if not chk["hits"]
            else "which of the buyer's identifiers were on those merchants' abuse lists (above)"
        )
        sequence = (
            ", and the payment was authorised first and captured only after the check"
            if ids["capture_id"]
            else ""
        )
        parts.append(
            f"What this shows: {listed} at the time of "
            f"purchase{sequence}. It is not proof of the buyer's identity."
        )
    head = " ".join(p for p in parts if p)
    if rcp["count"]:
        with_sessions = "; ".join(
            f"{r['receipt_id']} (session {r['ap3_session_id']})" for r in rcp["items"]
        )
        ids_only = ", ".join(r["receipt_id"] for r in rcp["items"])
        lead = f"\n\nSigned check receipts, {rcp['verified']} of {rcp['count']} verified: "
        for listing in (with_sessions, ids_only):
            if len(head) + len(lead) + len(listing) + 1 <= MAX_NOTE:
                return head + lead + listing + "."
    return fit_note(head)


def fit_note(note: str) -> str:
    """At most MAX_NOTE characters, cut at a sentence or line end where possible."""
    if len(note) <= MAX_NOTE:
        return note
    cut = note[:MAX_NOTE]
    end = max(cut.rfind("\n"), cut.rfind(". ") + 1)
    return (cut[:end] if end > MAX_NOTE // 2 else cut).rstrip()


def clean_note(text: str) -> str:
    """Plain text: tidy whitespace, drop Markdown emphasis and heading marks, keep paragraphs."""
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")]
    lines = [re.sub(r"^#+\s*", "", line) for line in lines]
    note = re.sub(r"\*\*|__", "", "\n".join(lines)).strip()
    return fit_note(re.sub(r"\n{3,}", "\n\n", note))


def _model(settings: Settings):
    if settings.dispute_model is not None:
        return settings.dispute_model
    from langchain.chat_models import init_chat_model

    return init_chat_model(
        f"{settings.llm_provider}:{settings.llm_model_smart}",
        timeout=settings.dispute_timeout_s,
        max_retries=1,
        # Sonnet thinks before it writes, and the thinking counts against max_tokens: at 2048 the
        # first live draft (2026-10-14) stopped mid-word. A cut-off reply goes to the template.
        max_tokens=8192,
    )


def _text(response) -> str:
    text = getattr(response, "text", None)
    return str(text) if text is not None else str(getattr(response, "content", ""))


async def write_draft(settings: Settings, facts: dict, order_id: str) -> tuple[str, str]:
    """(note, source), where source is "llm" or "template". Never raises."""
    try:
        model = _model(settings)
        messages = [("system", PROMPT_PATH.read_text()), ("human", json.dumps(facts, indent=1))]
        response = await asyncio.wait_for(
            model.ainvoke(messages), timeout=settings.dispute_timeout_s
        )
        if (getattr(response, "response_metadata", None) or {}).get("stop_reason") == "max_tokens":
            logger.warning("order %s: dispute reply cut off; using the template", order_id)
            return template_note(facts), "template"
        note = clean_note(_text(response))
        if note:
            return note, "llm"
        logger.warning("order %s: dispute model gave no text; using the template", order_id)
    except Exception as exc:  # any model, network or timeout error: the template stands in
        logger.warning(
            "order %s: dispute model failed (%s); using the template", order_id, type(exc).__name__
        )
    return template_note(facts), "template"


async def draft_dispute(app_state, dispute_id: str) -> None:
    """Draft the evidence note once per matched, unsubmitted dispute. The claim (draft_source
    "pending", one conditional update) stops two webhooks drafting the same dispute twice."""
    engine: Engine = app_state.engine
    stale = datetime.now(UTC) - STALE_DRAFT
    claimable = or_(
        disputes.c.draft_source.is_(None),
        and_(disputes.c.draft_source == "pending", disputes.c.updated_at < stale),
    )
    with engine.begin() as conn:
        claimed = conn.execute(
            update(disputes)
            .where(
                disputes.c.id == dispute_id,
                disputes.c.order_id.is_not(None),
                disputes.c.submitted_at.is_(None),
                claimable,
            )
            .values(draft_source="pending")
        ).rowcount
        if not claimed:
            return
        row = load_dispute(conn, dispute_id)
    order_id = row.order_id
    try:
        with engine.connect() as conn:
            facts = fact_sheet(conn, row)
        note, source = await write_draft(app_state.settings, facts, order_id)
    except BaseException:
        with engine.begin() as conn:
            conn.execute(
                update(disputes)
                .where(disputes.c.id == dispute_id, disputes.c.draft_source == "pending")
                .values(draft_source=None)
            )
        raise
    with engine.begin() as conn:
        conn.execute(
            update(disputes)
            .where(disputes.c.id == dispute_id)
            .values(draft_note=note, draft_source=source, drafted_at=utcnow())
        )
        add_event(
            conn,
            "dispute.drafted",
            order_id,
            {"dispute_id": dispute_id, "reason": row.reason, "draft_source": source},
            resource_id=dispute_id,
        )
    logger.info("order %s: dispute %s drafted (%s)", order_id, safe_id(dispute_id), source)


async def sync_dispute(app_state, dispute_id: str, resource: dict) -> None:
    """After a verified dispute webhook's 200: read, match, store, then draft. Errors are
    logged (names only) and never reach PayPal; the next dispute webhook tries again."""
    engine: Engine = app_state.engine
    paypal: PayPalClient = app_state.paypal
    label = safe_id(dispute_id)
    try:
        try:
            dispute = await paypal.get_dispute(dispute_id)
        except PayPalError as exc:
            logger.warning("dispute %s: read failed (%s); using the webhook", label, exc.name)
            dispute = resource
        with engine.connect() as conn:
            order_id = match_order(conn, dispute) or match_order(conn, resource)
        upsert_dispute(engine, dispute_id, dispute, order_id)
        if order_id is None:
            logger.warning("dispute %s: matches none of our orders; stored only", label)
            return
        logger.info("order %s: dispute %s stored (%s)", order_id, label, dispute.get("status"))
        await draft_dispute(app_state, dispute_id)
    except Exception:
        logger.exception("dispute %s: sync failed", label)


# Submitting (a reviewer's click, POST /merchant/disputes/{id}/evidence)


async def submit_evidence(engine: Engine, paypal: PayPalClient, dispute_id: str, note: str) -> dict:
    """Send the reviewer's note to PayPal as evidence. Re-reads the dispute first: PayPal takes
    evidence only while it is WAITING_FOR_SELLER_RESPONSE with a provide-evidence link (409
    `dispute_not_awaiting_evidence` otherwise). A repeat call returns the stored submission."""
    with engine.connect() as conn:
        row = load_dispute(conn, dispute_id)
    if row.submitted_at:
        return dispute_body(row)
    try:
        dispute = await paypal.get_dispute(dispute_id)
    except PayPalError as exc:
        raise paypal_error(engine, row.order_id, "get_dispute", exc) from None
    upsert_dispute(engine, dispute_id, dispute, None)
    status = dispute.get("status")
    if status != AWAITING_SELLER or not can_provide_evidence(dispute):
        raise APIError(
            409,
            "dispute_not_awaiting_evidence",
            f"PayPal dispute is {status}; evidence can be sent only when PayPal asks the seller.",
            {"dispute_status": status},
        )
    try:
        await paypal.provide_evidence(dispute_id, note, request_id=f"{dispute_id}:evidence")
    except PayPalError as exc:
        raise paypal_error(engine, row.order_id, "provide_evidence", exc) from None
    with engine.begin() as conn:
        stored = conn.execute(
            update(disputes)
            .where(disputes.c.id == dispute_id, disputes.c.submitted_at.is_(None))
            .values(submitted_note=note, submitted_at=utcnow(), submitted_by="reviewer")
        ).rowcount
        if stored:
            add_event(
                conn,
                "dispute.evidence_submitted",
                row.order_id,
                {"dispute_id": dispute_id, "actor": "reviewer", "dispute_status": status},
                resource_id=dispute_id,
            )
    logger.info("order %s: dispute %s evidence submitted", row.order_id, safe_id(dispute_id))
    try:  # PayPal moves the dispute on (usually UNDER_REVIEW); best effort
        upsert_dispute(engine, dispute_id, await paypal.get_dispute(dispute_id), None)
    except PayPalError:
        pass
    with engine.connect() as conn:
        return dispute_body(load_dispute(conn, dispute_id))
