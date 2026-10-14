"""POST /webhooks/paypal: verify, store, dedupe, act (docs/contracts.md, "Webhook events handled").

Every delivery is verified with PayPal's verify-webhook-signature API, then stored in `events`.
Only verified events act or deduplicate: an unverified event is stored with `paypal_event_id`
NULL (its claimed ID stays in the payload), so a forged delivery can't claim a real event's ID.
"""

import json
import logging

from fastapi import APIRouter, Request
from sqlalchemy import Connection, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import events, orders
from app.errors import APIError
from app.ids import new_id
from app.payments import HONOR_PERIOD, get_paypal, parse_time
from app.paypal import PayPalError

logger = logging.getLogger("quietcheck.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 256 * 1024


def find_order_id(conn: Connection, event_type: str, resource: dict) -> str | None:
    """Our order for this event: the PayPal order ID on CHECKOUT.ORDER.*; otherwise
    `custom_id` (our order ID, copied from the purchase unit) or the related PayPal order."""
    candidates = []
    if event_type.startswith("CHECKOUT.ORDER."):
        candidates.append(orders.c.paypal_order_id == resource.get("id"))
    if resource.get("custom_id"):
        candidates.append(orders.c.id == resource["custom_id"])
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    if related.get("order_id"):
        candidates.append(orders.c.paypal_order_id == related["order_id"])
    if not candidates:
        return None
    row = conn.execute(select(orders.c.id).where(or_(*candidates))).first()
    return row.id if row else None


def apply_event(conn: Connection, event_type: str, order_id: str, resource: dict) -> None:
    """The contract's actions. Each one only moves an order along an allowed transition."""
    order = orders.c
    if event_type == "CHECKOUT.ORDER.APPROVED":
        # D4 adds the /merchant/process trigger here.
        conn.execute(
            update(orders)
            .where(order.id == order_id, order.status == "created")
            .values(status="approved")
        )
    elif event_type == "PAYMENT.AUTHORIZATION.CREATED":
        created = parse_time(resource.get("create_time"))
        conn.execute(
            update(orders)
            .where(order.id == order_id, order.authorization_id.is_(None))
            .values(
                authorization_id=resource.get("id"),
                authorization_expires_at=parse_time(resource.get("expiration_time")),
                honor_period_ends_at=created + HONOR_PERIOD if created else None,
            )
        )
    elif event_type == "PAYMENT.AUTHORIZATION.VOIDED":
        conn.execute(
            update(orders)
            .where(order.id == order_id, order.status == "held")
            .values(status="voided")
        )
    elif event_type == "PAYMENT.CAPTURE.COMPLETED":
        conn.execute(
            update(orders)
            .where(order.id == order_id, order.capture_id.is_(None))
            .values(capture_id=resource.get("id"))
        )
        conn.execute(
            update(orders)
            .where(order.id == order_id, order.status.in_(("checking", "held")))
            .values(status="captured")
        )
    # CUSTOMER.DISPUTE.CREATED and anything else: store only.


@router.post("/paypal")
async def paypal_webhook(request: Request) -> dict:
    engine = request.app.state.engine
    settings = request.app.state.settings
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise APIError(400, "bad_request", "Webhook body too large.")
    try:
        event = json.loads(raw)
    except ValueError:
        raise APIError(400, "bad_request", "Malformed JSON body.") from None
    if not isinstance(event, dict) or not event.get("id") or not event.get("event_type"):
        raise APIError(400, "bad_request", "Not a PayPal webhook event.")
    event_type = str(event["event_type"])
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}

    try:
        verified = await get_paypal(request).verify_webhook_signature(
            settings.paypal_webhook_id, request.headers, raw
        )
    except PayPalError as exc:
        # Not stored: answering non-2xx makes PayPal redeliver, and we verify it then.
        raise APIError(
            502, "paypal_error", f"Webhook verification failed: {exc.name}.", exc.details
        ) from None

    with engine.begin() as conn:
        order_id = find_order_id(conn, event_type, resource) if verified else None
        row = {
            "id": new_id("evt"),
            "source": "paypal_webhook",
            "paypal_event_id": str(event["id"]) if verified else None,
            "event_type": event_type,
            "order_id": order_id,
            "resource_id": resource.get("id"),
            "verified": verified,
            "payload": event,
        }
        if not verified:
            conn.execute(insert(events).values(row))
        else:
            try:
                with conn.begin_nested():
                    conn.execute(insert(events).values(row))
            except IntegrityError:
                logger.info("webhook %s %s is a duplicate", event_type, event["id"])
                return {"received": True}
            if order_id:
                apply_event(conn, event_type, order_id, resource)

    if not verified:
        logger.warning("webhook %s failed verification", event_type)
        raise APIError(400, "webhook_unverified", "Webhook signature verification failed.")
    logger.info("webhook %s %s for order %s", event_type, event["id"], order_id)
    return {"received": True}
