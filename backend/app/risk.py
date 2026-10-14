"""POST /risk/check: the AP3 private check (docs/contracts.md)."""

import logging
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
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


class ShippingAddress(BaseModel):
    """PayPal's order `shipping.address` field names. Missing parts normalise to ""."""

    address_line_1: str | None = None
    address_line_2: str | None = None
    admin_area_2: str | None = None
    admin_area_1: str | None = None
    postal_code: str | None = None
    country_code: str | None = None


class Identifiers(BaseModel):
    email: str = Field(min_length=1)
    shipping_address: ShippingAddress
    device_id: str = Field(min_length=1)


class CheckRequest(BaseModel):
    order_id: str = Field(min_length=1)
    identifiers: Identifiers


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
    """Return (check_row, run). run is False when a completed check should just be returned."""
    with engine.begin() as conn:
        if conn.execute(select(orders.c.id).where(orders.c.id == order_id)).first() is None:
            raise APIError(404, "not_found", f"Order {order_id} not found.")
        existing = conn.execute(select(checks).where(checks.c.order_id == order_id)).first()
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
                return conn.execute(select(checks).where(checks.c.id == check_id)).first(), True
    if existing.status == "completed":
        return existing, False
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
        return conn.execute(select(checks).where(checks.c.id == existing.id)).first(), True


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
    engine: Engine = request.app.state.engine
    initiator: Initiator | None = request.app.state.initiator
    if initiator is None:
        raise APIError(502, "member_unavailable", "The AP3 initiator is not running.")

    check_row, run = _claim_check(engine, body.order_id)
    if not run:
        return check_response(engine, check_row)

    ids = body.identifiers
    hashes = {
        "email": identifier_hash("email", ids.email),
        "shipping_address": identifier_hash("shipping_address", ids.shipping_address.model_dump()),
        "device_id": identifier_hash("device_id", ids.device_id),
    }
    started = time.perf_counter()
    try:
        results = await initiator.run_check(
            hashes, timeout_s=request.app.state.settings.check_timeout_s
        )
    except ConsortiumError as exc:
        members = sorted({m for m, _f, _e in exc.failures})
        _fail_check(engine, check_row.id, body.order_id, "member_unavailable", members)
        raise APIError(
            502,
            "member_unavailable",
            f"Consortium member(s) {', '.join(members)} didn't complete a PSI session.",
            {"check_id": check_row.id, "members": members},
        ) from None
    except BaseException:
        # Includes cancellation: leave the check `failed` so it can be rerun, never `running`.
        _fail_check(engine, check_row.id, body.order_id, "internal_error")
        raise
    _complete_check(engine, check_row.id, body.order_id, results)
    logger.info(
        "check %s for %s: %d sessions in %.2fs, hit_count=%d",
        check_row.id,
        body.order_id,
        len(results),
        time.perf_counter() - started,
        sum(r.is_match for r in results),
    )
    with engine.connect() as conn:
        check_row = conn.execute(select(checks).where(checks.c.id == check_row.id)).first()
    return check_response(engine, check_row)
