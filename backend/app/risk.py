"""POST /risk/check: the AP3 private check (docs/contracts.md)."""

import logging
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.config import MEMBERS
from app.consortium.initiator import ConsortiumError, Initiator
from app.db import checks, events, orders, receipts, utcnow
from app.errors import APIError
from app.identifiers import FIELDS, identifier_hash
from app.ids import new_id

logger = logging.getLogger("quietcheck.risk")

router = APIRouter(prefix="/risk", tags=["risk"])


class CheckRequest(BaseModel):
    """Only the order ID: the identifiers come from the stored order, never from the caller,
    so a caller can't check chosen values or swap in clean ones for a risky order."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)


def order_identifiers(order_row) -> dict:
    """The order's payer email and shipping address (from PayPal after approval) and the
    device ID sent at order creation. 409 if any is missing: the order isn't ready."""
    identifiers = {
        "email": order_row.payer_email,
        "shipping_address": order_row.shipping_address,
        "device_id": order_row.device_id,
    }
    missing = [f for f in FIELDS if not identifiers[f]]
    if missing:
        raise APIError(
            409,
            "invalid_state",
            f"Order {order_row.id} has no {', '.join(missing)} yet; it can't be checked.",
            {"missing": missing},
        )
    return identifiers


_ORDER = {(m, f): i for i, (m, f) in enumerate((m, f) for m in MEMBERS for f in FIELDS)}


def check_response(engine: Engine, check_row) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(select(receipts).where(receipts.c.check_id == check_row.id)).all()
    rows = sorted(rows, key=lambda r: _ORDER[(r.member, r.field)])
    return {
        "check_id": check_row.id,
        "order_id": check_row.order_id,
        "hits": [{"member": r.member, "field": r.field, "is_match": r.is_match} for r in rows],
        "hit_count": check_row.hit_count,
        "receipts": [
            {
                "receipt_id": r.id,
                "member": r.member,
                "field": r.field,
                "session_id": r.session_id,
                "verified": r.verified,
            }
            for r in rows
        ],
    }


def _claim_check(engine: Engine, order_id: str):
    """Return (check_row, identifiers). identifiers is None when a completed check should just
    be returned; otherwise this caller owns the (new or rerun) check and must run it."""
    with engine.begin() as conn:
        order_row = conn.execute(select(orders).where(orders.c.id == order_id)).first()
        if order_row is None:
            raise APIError(404, "not_found", f"Order {order_id} not found.")
        existing = conn.execute(select(checks).where(checks.c.order_id == order_id)).first()
        if existing is not None and existing.status == "completed":
            return existing, None
        identifiers = order_identifiers(order_row)
        if existing is None:
            check_id = new_id("chk")
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(checks).values(
                            id=check_id, order_id=order_id, status="running", started_at=utcnow()
                        )
                    )
            except IntegrityError:
                existing = conn.execute(select(checks).where(checks.c.order_id == order_id)).first()
            else:
                row = conn.execute(select(checks).where(checks.c.id == check_id)).first()
                return row, identifiers
    if existing.status == "completed":
        return existing, None
    if existing.status == "running":
        raise APIError(409, "invalid_state", f"A check for order {order_id} is already running.")
    # failed: rerun on the same check row, if no concurrent caller claims it first.
    with engine.begin() as conn:
        claimed = conn.execute(
            update(checks)
            .where(checks.c.id == existing.id, checks.c.status == "failed")
            .values(
                status="running", error=None, hit_count=None, started_at=utcnow(), completed_at=None
            )
        ).rowcount
        if not claimed:
            raise APIError(
                409, "invalid_state", f"A check for order {order_id} is already running."
            )
        conn.execute(delete(receipts).where(receipts.c.check_id == existing.id))
        row = conn.execute(select(checks).where(checks.c.id == existing.id)).first()
        return row, identifiers


def _fail_check(engine: Engine, check_id: str, order_id: str, code: str, members=()) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(checks)
            .where(checks.c.id == check_id)
            .values(status="failed", error=code, completed_at=utcnow())
        )
        conn.execute(
            insert(events).values(
                id=new_id("evt"),
                source="system",
                event_type="check.failed",
                order_id=order_id,
                payload={"check_id": check_id, "error": code, "members": list(members)},
            )
        )


def _complete_check(engine: Engine, check_id: str, order_id: str, results) -> None:
    hit_count = sum(r.is_match for r in results)
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(receipts),
            [
                {
                    "id": new_id("rcp"),
                    "check_id": check_id,
                    "member": r.member,
                    "field": r.field,
                    "is_match": r.is_match,
                    "session_id": r.session_id,
                    "intent_directives": r.intent_directives,
                    "result_directive": r.result_directive,
                    "member_commitment": r.member_commitment,
                    "member_public_key": r.member_public_key,
                    "verified": r.verified,
                    "created_at": now,
                }
                for r in results
            ],
        )
        conn.execute(
            update(checks)
            .where(checks.c.id == check_id)
            .values(status="completed", hit_count=hit_count, completed_at=now)
        )
        conn.execute(
            insert(events).values(
                id=new_id("evt"),
                source="system",
                event_type="check.completed",
                order_id=order_id,
                payload={
                    "check_id": check_id,
                    "hit_count": hit_count,
                    "verified": all(r.verified for r in results),
                },
            )
        )


@router.post("/check")
async def risk_check(body: CheckRequest, request: Request) -> dict:
    return await run_risk_check(
        request.app.state.engine,
        request.app.state.initiator,
        body.order_id,
        timeout_s=request.app.state.settings.check_timeout_s,
    )


def stored_check(engine: Engine, order_id: str) -> dict | None:
    """The order's completed check as the /risk/check response, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            select(checks).where(checks.c.order_id == order_id, checks.c.status == "completed")
        ).first()
    return check_response(engine, row) if row else None


async def run_risk_check(
    engine: Engine, initiator: Initiator | None, order_id: str, timeout_s: float
) -> dict:
    """The /risk/check action (also called by /merchant/process). Raises APIError.

    A completed check is returned as stored, even without a running initiator."""
    stored = stored_check(engine, order_id)
    if stored is not None:
        return stored
    if initiator is None:
        raise APIError(502, "member_unavailable", "The AP3 initiator is not running.")

    check_row, identifiers = _claim_check(engine, order_id)
    if identifiers is None:
        return check_response(engine, check_row)

    hashes = {field: identifier_hash(field, identifiers[field]) for field in FIELDS}
    started = time.perf_counter()
    try:
        results = await initiator.run_check(hashes, timeout_s=timeout_s)
    except ConsortiumError as exc:
        members = sorted({m for m, _f, _e in exc.failures})
        _fail_check(engine, check_row.id, order_id, "member_unavailable", members)
        raise APIError(
            502,
            "member_unavailable",
            f"Consortium member(s) {', '.join(members)} didn't complete a PSI session.",
            {"check_id": check_row.id, "members": members},
        ) from None
    except BaseException:
        # Includes cancellation: leave the check `failed` so it can be rerun, never `running`.
        _fail_check(engine, check_row.id, order_id, "internal_error")
        raise
    _complete_check(engine, check_row.id, order_id, results)
    logger.info(
        "check %s for %s: %d sessions in %.2fs, hit_count=%d",
        check_row.id,
        order_id,
        len(results),
        time.perf_counter() - started,
        sum(r.is_match for r in results),
    )
    with engine.connect() as conn:
        check_row = conn.execute(select(checks).where(checks.c.id == check_row.id)).first()
    return check_response(engine, check_row)
