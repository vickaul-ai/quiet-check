"""/merchant: the risk agent and the read endpoints (docs/contracts.md; D4).

`process_order` takes an approved order to a decision: read the payer details from PayPal, claim
the order (`-> checking`, one conditional update, so the losing trigger gets 202), run the AP3
check, apply the policy, have the model write the rationale, then `decide_order`.

`decide_order` recomputes the policy from the stored receipts (`409 policy_mismatch` on any
other decision), writes the decision row and its `decision.made` event, then runs the branch's
PayPal action. The decision row comes first because the payment guards need it (ADR 0007). If a
PayPal call fails after that, the order goes to `failed` with the decision kept; rerunning
`/merchant/process` reuses the stored check and decision and resumes the PayPal action
(decided with Vic, 2026-10-13; ADR 0008).
"""

import logging
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, desc, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import checks, decisions, events, orders, receipts
from app.errors import APIError
from app.ids import new_id
from app.payments import (
    add_event,
    authorize_order,
    capture_authorization,
    fetch_payer_details,
    iso_z,
    last_event_payload,
    load_order,
    money,
)
from app.paypal import PayPalClient
from app.policy import PAYPAL_ACTIONS
from app.policy import decide as apply_policy
from app.rationale import write_rationale
from app.risk import check_response, run_risk_check

logger = logging.getLogger("quietcheck.merchant")

router = APIRouter(prefix="/merchant", tags=["merchant"])

CLAIMABLE = ("created", "approved", "failed")
DECIDED = ("captured", "held", "blocked", "voided")
# PayPal order states a run can start from: APPROVED, or COMPLETED when an earlier run already
# authorised (a rerun after a failed capture).
PAYPAL_READY = ("APPROVED", "COMPLETED")
ORDERS_LIMIT = 100
EVENTS_LIMIT = 50

WHO_SAW_WHAT = [
    {
        "party": "Merchant (us)",
        "sent": "Blinded hashes of email, address, device",
        "learned": "Match yes/no per member per field",
        "never_saw": "Any member's list",
    },
    *[
        {
            "party": f"Member {m}",
            "sent": "Its PSI responses (unsigned) and its signed commitment",
            "learned": "Nothing about the query or result",
            "never_saw": "The buyer's identifiers; other members' lists",
        }
        for m in ("A", "B", "C")
    ],
    {
        "party": "PayPal",
        "sent": "Payer details; payment status",
        "learned": "Order, authorisation, capture or void",
        "never_saw": "Any fraud list or check result",
    },
]


class ProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)
    decision: Literal["clean", "hold", "block"]
    rationale: str = Field(min_length=1, max_length=2000)
    check_id: str = Field(min_length=1)
    rationale_source: Literal["llm", "template"] = "llm"


def _set_status(engine: Engine, order_id: str, from_statuses: tuple[str, ...], to: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            update(orders)
            .where(orders.c.id == order_id, orders.c.status.in_(from_statuses))
            .values(status=to)
        ).rowcount


def _decision_row(engine: Engine, order_id: str):
    with engine.connect() as conn:
        return conn.execute(select(decisions).where(decisions.c.order_id == order_id)).first()


def _process_response(engine: Engine, order_id: str) -> dict:
    with engine.connect() as conn:
        order = load_order(conn, order_id)
        decision = conn.execute(select(decisions).where(decisions.c.order_id == order_id)).first()
        check = conn.execute(select(checks.c.id).where(checks.c.order_id == order_id)).first()
    return {
        "order_id": order_id,
        "status": order.status,
        "decision": decision.decision if decision else None,
        "decision_id": decision.id if decision else None,
        "check_id": check.id if check else None,
    }


# POST /merchant/decide


def _stored_hits(engine: Engine, order_id: str, check_id: str) -> tuple[object, list[dict]]:
    with engine.connect() as conn:
        check = conn.execute(select(checks).where(checks.c.id == check_id)).first()
        if check is None or check.order_id != order_id:
            raise APIError(404, "not_found", f"Check {check_id} not found for order {order_id}.")
        if check.status != "completed":
            raise APIError(
                409,
                "invalid_state",
                f"Check {check_id} is {check.status}; a decision needs a completed check.",
                {"check_status": check.status},
            )
        rows = conn.execute(select(receipts).where(receipts.c.check_id == check_id)).all()
    return check, [{"member": r.member, "field": r.field, "is_match": r.is_match} for r in rows]


def _decide_response(engine: Engine, order_id: str) -> dict:
    with engine.connect() as conn:
        order = load_order(conn, order_id)
        row = conn.execute(select(decisions).where(decisions.c.order_id == order_id)).first()
    return {
        "decision_id": row.id,
        "order_id": order_id,
        "decision": row.decision,
        "paypal_action": row.paypal_action,
        "status": order.status,
        "authorization_id": order.authorization_id,
        "capture_id": order.capture_id,
    }


async def decide_order(
    engine: Engine,
    paypal: PayPalClient,
    order_id: str,
    decision: str,
    rationale: str,
    check_id: str,
    rationale_source: str = "llm",
) -> dict:
    """Record the decision and run its PayPal action. Raises APIError."""
    with engine.connect() as conn:
        order = load_order(conn, order_id)
    check, hits = _stored_hits(engine, order_id, check_id)
    policy_decision = apply_policy(hits)
    if decision != policy_decision:
        logger.warning(
            "order %s: decision %s rejected (policy: %s)", order_id, decision, policy_decision
        )
        raise APIError(
            409,
            "policy_mismatch",
            f"Decision {decision} differs from the policy result {policy_decision}.",
            {"decision": decision, "policy_decision": policy_decision},
        )

    existing = _decision_row(engine, order_id)
    if existing is None:
        if order.status != "checking":
            raise APIError(
                409,
                "invalid_state",
                f"Order {order_id} is {order.status}; a decision needs checking.",
                {"status": order.status},
            )
        decision_id = new_id("dec")
        try:
            with engine.begin() as conn:
                conn.execute(
                    insert(decisions).values(
                        id=decision_id,
                        order_id=order_id,
                        check_id=check_id,
                        decision=decision,
                        rationale=rationale,
                        paypal_action=PAYPAL_ACTIONS[decision],
                    )
                )
                add_event(
                    conn,
                    "decision.made",
                    order_id,
                    {
                        "decision_id": decision_id,
                        "decision": decision,
                        "check_id": check_id,
                        "hit_count": check.hit_count,
                        "paypal_action": PAYPAL_ACTIONS[decision],
                        "rationale_source": rationale_source,
                    },
                )
        except IntegrityError:  # a concurrent call recorded it first; carry on as a resume
            pass
        logger.info("order %s decided %s (%s rationale)", order_id, decision, rationale_source)

    with engine.connect() as conn:
        order = load_order(conn, order_id)
    if order.status == "checking":
        await _run_paypal_action(engine, paypal, order_id, decision)
    return _decide_response(engine, order_id)


async def _run_paypal_action(engine: Engine, paypal: PayPalClient, order_id: str, decision: str):
    """The branch's PayPal action. On failure the order goes to `failed` (decision kept)."""
    try:
        if decision == "block":
            _set_status(engine, order_id, ("checking",), "blocked")
            return
        auth = await authorize_order(engine, paypal, order_id)
        if decision == "clean":
            await capture_authorization(engine, paypal, auth["authorization_id"], "policy", None)
        else:
            _set_status(engine, order_id, ("checking",), "held")
    except BaseException:
        _set_status(engine, order_id, ("checking",), "failed")
        raise
    finally:
        with engine.connect() as conn:
            status = load_order(conn, order_id).status
        logger.info("order %s is %s after the %s branch", order_id, status, decision)


@router.post("/decide")
async def decide(body: DecideRequest, request: Request) -> dict:
    return await decide_order(
        request.app.state.engine,
        request.app.state.paypal,
        body.order_id,
        body.decision,
        body.rationale,
        body.check_id,
        body.rationale_source,
    )


# POST /merchant/process


async def process_order(app_state, order_id: str) -> dict | None:
    """Run the risk agent on one order. Returns the 200 body, or None when another run holds
    the order (`checking`: the caller answers 202). Raises APIError."""
    engine: Engine = app_state.engine
    with engine.connect() as conn:
        status = load_order(conn, order_id).status
    if status in DECIDED:
        return _process_response(engine, order_id)
    if status == "checking":
        return None

    paypal_order = await fetch_payer_details(engine, app_state.paypal, order_id)
    if paypal_order.get("status") not in PAYPAL_READY:
        raise APIError(
            409,
            "invalid_state",
            f"PayPal order is {paypal_order.get('status')}; the buyer hasn't approved it.",
            {"paypal_status": paypal_order.get("status")},
        )
    if not _set_status(engine, order_id, CLAIMABLE, "checking"):
        with engine.connect() as conn:
            status = load_order(conn, order_id).status
        return _process_response(engine, order_id) if status in DECIDED else None
    logger.info("order %s: risk agent started", order_id)

    try:
        check = await run_risk_check(
            engine, app_state.initiator, order_id, timeout_s=app_state.settings.check_timeout_s
        )
        decision = apply_policy(check["hits"])
        stored = _decision_row(engine, order_id)
        if stored is not None:  # a rerun after a failed PayPal call: no new rationale
            rationale, source = stored.rationale, "stored"
        else:
            rationale, source = await write_rationale(
                app_state.settings, decision, check["hits"], order_id
            )
    except BaseException as exc:
        _set_status(engine, order_id, ("checking",), "failed")
        logger.warning(
            "order %s: run failed (%s)", order_id, getattr(exc, "code", type(exc).__name__)
        )
        raise
    await decide_order(
        engine, app_state.paypal, order_id, decision, rationale, check["check_id"], source
    )
    return _process_response(engine, order_id)


@router.post("/process")
async def process(body: ProcessRequest, request: Request):
    result = await process_order(request.app.state, body.order_id)
    if result is None:
        return JSONResponse(
            status_code=202, content={"order_id": body.order_id, "status": "checking"}
        )
    return result


async def process_in_background(app_state, order_id: str) -> None:
    """The CHECKOUT.ORDER.APPROVED trigger, run after the webhook's 200. Errors are logged
    (codes only); the order is left `failed` for a rerun."""
    try:
        result = await process_order(app_state, order_id)
    except APIError as exc:
        logger.warning("order %s: background process failed: %s", order_id, exc.code)
    except Exception:
        logger.exception("order %s: background process crashed", order_id)
    else:
        state = result["status"] if result else "already running"
        logger.info("order %s: background process done (%s)", order_id, state)


# Read endpoints


def _summary(order, decision, hit_count) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "decision": decision,
        "hit_count": hit_count,
        "amount": money(order.amount_value, order.currency_code),
        "paypal_order_id": order.paypal_order_id,
        "authorization_id": order.authorization_id,
        "capture_id": order.capture_id,
        "honor_period_ends_at": iso_z(order.honor_period_ends_at),
        "created_at": iso_z(order.created_at),
    }


@router.get("/orders")
def list_orders(request: Request) -> dict:
    engine: Engine = request.app.state.engine
    query = (
        select(orders, decisions.c.decision, checks.c.hit_count)
        .outerjoin(decisions, decisions.c.order_id == orders.c.id)
        .outerjoin(checks, checks.c.order_id == orders.c.id)
        .order_by(desc(orders.c.id))
        .limit(ORDERS_LIMIT)
    )
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"orders": [_summary(r, r.decision, r.hit_count) for r in rows]}


def _receipt(row) -> dict:
    return {
        "receipt_id": row.id,
        "check_id": row.check_id,
        "member": row.member,
        "field": row.field,
        "is_match": row.is_match,
        "session_id": row.session_id,
        "intent_directives": row.intent_directives,
        "result_directive": row.result_directive,
        "member_commitment": row.member_commitment,
        "member_public_key": row.member_public_key,
        "verified": row.verified,
        "created_at": iso_z(row.created_at),
    }


def _decision(row, rationale_source: str | None) -> dict:
    return {
        "decision_id": row.id,
        "check_id": row.check_id,
        "decision": row.decision,
        "rationale": row.rationale,
        "rationale_source": rationale_source,
        "paypal_action": row.paypal_action,
        "resolution": row.resolution,
        "resolution_note": row.resolution_note,
        "resolved_at": iso_z(row.resolved_at),
        "created_at": iso_z(row.created_at),
    }


@router.get("/orders/{order_id}")
def get_order(order_id: str, request: Request) -> dict:
    engine: Engine = request.app.state.engine
    with engine.connect() as conn:
        order = load_order(conn, order_id)
        check = conn.execute(select(checks).where(checks.c.order_id == order_id)).first()
        decision = conn.execute(select(decisions).where(decisions.c.order_id == order_id)).first()
        receipt_rows = (
            conn.execute(select(receipts).where(receipts.c.check_id == check.id)).all()
            if check
            else []
        )
        made = last_event_payload(conn, order_id, "decision.made")
    check_body = check_response(engine, check) if check and check.status == "completed" else None
    order_rank = {r["receipt_id"]: i for i, r in enumerate((check_body or {}).get("receipts", []))}
    receipt_rows = sorted(receipt_rows, key=lambda r: order_rank.get(r.id, 0))
    summary = _summary(
        order, decision.decision if decision else None, check.hit_count if check else None
    )
    return {
        **summary,
        "items": order.items,
        "device_id": order.device_id,
        "payer_email": order.payer_email,
        "shipping_address": order.shipping_address,
        "check": check_body,
        "receipts": [_receipt(r) for r in receipt_rows],
        "decision": _decision(decision, made.get("rationale_source")) if decision else None,
        "who_saw_what": WHO_SAW_WHAT,
    }


@router.get("/orders/{order_id}/status")
def get_order_status(order_id: str, request: Request) -> dict:
    """Status only, for the storefront to poll: no payer details, receipts or decision."""
    with request.app.state.engine.connect() as conn:
        order = load_order(conn, order_id)
    return {"order_id": order.id, "status": order.status}


def _event(row) -> dict:
    return {
        "event_id": row.id,
        "source": row.source,
        "event_type": row.event_type,
        "order_id": row.order_id,
        "paypal_event_id": row.paypal_event_id,
        "resource_id": row.resource_id,
        "verified": row.verified,
        "payload": row.payload,
        "created_at": iso_z(row.created_at),
    }


@router.get("/events")
def list_events(request: Request, after: str | None = Query(default=None, max_length=64)) -> dict:
    # Unverified webhook deliveries stay stored for the record but out of the feed: the webhook
    # URL is public, so anyone can post them (D9).
    query = (
        select(events)
        .where(or_(events.c.verified.is_(None), events.c.verified.is_(True)))
        .order_by(events.c.id)
        .limit(EVENTS_LIMIT)
    )
    if after:
        query = query.where(events.c.id > after)
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"events": [_event(r) for r in rows]}
