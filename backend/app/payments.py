"""/payments: create order, authorise, capture, void (docs/contracts.md).

Status guards (D3 decision, "strict"): authorise needs our order in `checking` and PayPal's in
`APPROVED`; a policy capture needs `checking` plus a stored `clean` decision; a reviewer capture
or void needs `held`. Every endpoint returns the stored result if its action already happened,
without calling PayPal again.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, desc, insert, select, update

from app.catalog import load_catalog
from app.db import decisions, events, orders, utcnow
from app.errors import APIError
from app.ids import new_id
from app.paypal import PayPalClient, PayPalError

logger = logging.getLogger("quietcheck.payments")

router = APIRouter(prefix="/payments", tags=["payments"])

HONOR_PERIOD = timedelta(days=3)


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=10)


class CreateOrderRequest(BaseModel):
    """SKUs and quantities only: the backend prices them (no prices from the agent)."""

    model_config = ConfigDict(extra="forbid")

    items: list[OrderItem] = Field(min_length=1, max_length=20)
    device_id: str = Field(min_length=1, max_length=128)


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Literal["policy", "reviewer"]
    note: str | None = Field(default=None, max_length=1000)


VoidRequest = CaptureRequest


def iso_z(value: datetime | None) -> str | None:
    """ISO 8601 UTC with a Z. SQLite hands timestamps back naive; they are stored as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def money(value: Decimal | str, currency_code: str = "USD") -> dict:
    return {"currency_code": currency_code, "value": f"{Decimal(value):.2f}"}


def add_event(
    conn: Connection,
    event_type: str,
    order_id: str | None,
    payload: dict,
    resource_id: str | None = None,
) -> None:
    conn.execute(
        insert(events).values(
            id=new_id("evt"),
            source="system",
            event_type=event_type,
            order_id=order_id,
            resource_id=resource_id,
            payload=payload,
        )
    )


def last_event_payload(conn: Connection, order_id: str, event_type: str) -> dict:
    row = conn.execute(
        select(events.c.payload)
        .where(events.c.order_id == order_id, events.c.event_type == event_type)
        .order_by(desc(events.c.id))
        .limit(1)
    ).first()
    return row.payload if row else {}


def paypal_error(engine: Engine, order_id: str, action: str, exc: PayPalError) -> APIError:
    """Record the failure on the live feed and build the contract's 502."""
    with engine.begin() as conn:
        add_event(conn, "paypal.error", order_id, {"action": action, **exc.details})
    return APIError(502, "paypal_error", f"PayPal {action} failed: {exc.name}.", exc.details)


def get_paypal(request: Request) -> PayPalClient:
    return request.app.state.paypal


def load_order(conn: Connection, order_id: str):
    row = conn.execute(select(orders).where(orders.c.id == order_id)).first()
    if row is None:
        raise APIError(404, "not_found", f"Order {order_id} not found.")
    return row


def order_by_authorization(conn: Connection, authorization_id: str):
    row = conn.execute(select(orders).where(orders.c.authorization_id == authorization_id)).first()
    if row is None:
        raise APIError(404, "not_found", f"Authorization {authorization_id} not found.")
    return row


def payer_details(paypal_order: dict) -> dict:
    """The payer email and shipping address from a PayPal order (after approval). The address
    is PayPal's `shipping.address` object, stored unchanged."""
    units = paypal_order.get("purchase_units") or [{}]
    return {
        "payer_email": (paypal_order.get("payer") or {}).get("email_address"),
        "shipping_address": (units[0].get("shipping") or {}).get("address"),
    }


def store_payer_details(conn: Connection, order_id: str, paypal_order: dict) -> None:
    """Fill payer_email and shipping_address from PayPal if they're still empty. D4's
    /merchant/process calls this before /risk/check (which reads them from the order)."""
    details = payer_details(paypal_order)
    for column in ("payer_email", "shipping_address"):
        if details[column]:
            conn.execute(
                update(orders)
                .where(orders.c.id == order_id, getattr(orders.c, column).is_(None))
                .values({column: details[column]})
            )


async def fetch_payer_details(engine: Engine, paypal: PayPalClient, order_id: str) -> dict:
    """GET the PayPal order and store its payer details on our order. Returns PayPal's order."""
    with engine.connect() as conn:
        order = load_order(conn, order_id)
    try:
        paypal_order = await paypal.get_order(order.paypal_order_id)
    except PayPalError as exc:
        raise paypal_error(engine, order_id, "get_order", exc) from None
    with engine.begin() as conn:
        store_payer_details(conn, order_id, paypal_order)
    return paypal_order


def first_authorization(paypal_order: dict) -> dict | None:
    for unit in paypal_order.get("purchase_units") or []:
        for auth in (unit.get("payments") or {}).get("authorizations") or []:
            return auth
    return None


# POST /payments/orders


def price_items(items: list[OrderItem]) -> tuple[list[dict], Decimal]:
    catalog = load_catalog()
    skus = [item.sku for item in items]
    if len(set(skus)) != len(skus):
        raise APIError(422, "validation_error", "Each SKU may appear once.", {"fields": ["items"]})
    priced, total = [], Decimal("0")
    for item in items:
        product = catalog.get(item.sku)
        if product is None:
            raise APIError(404, "not_found", f"Unknown SKU {item.sku}.", {"sku": item.sku})
        unit = Decimal(product["unit_amount"]["value"])
        total += unit * item.quantity
        priced.append(
            {
                "sku": item.sku,
                "name": product["name"],
                "quantity": item.quantity,
                "unit_amount": money(unit),
            }
        )
    return priced, total


def paypal_order_body(order_id: str, priced: list[dict], total: Decimal) -> dict:
    total_money = money(total)
    return {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "custom_id": order_id,
                "amount": {**total_money, "breakdown": {"item_total": total_money}},
                "items": [
                    {
                        "name": item["name"],
                        "sku": item["sku"],
                        "quantity": str(item["quantity"]),
                        "unit_amount": item["unit_amount"],
                        "category": "PHYSICAL_GOODS",
                    }
                    for item in priced
                ],
            }
        ],
    }


@router.post("/orders", status_code=201)
async def create_order(body: CreateOrderRequest, request: Request) -> dict:
    engine: Engine = request.app.state.engine
    priced, total = price_items(body.items)
    order_id = new_id("ord")
    try:
        paypal_order = await get_paypal(request).create_order(
            paypal_order_body(order_id, priced, total), request_id=f"{order_id}:create"
        )
    except PayPalError as exc:
        raise APIError(
            502, "paypal_error", f"PayPal create order failed: {exc.name}.", exc.details
        ) from None
    amount = money(total)
    with engine.begin() as conn:
        conn.execute(
            insert(orders).values(
                id=order_id,
                paypal_order_id=paypal_order["id"],
                status="created",
                items=priced,
                amount_value=amount["value"],
                currency_code=amount["currency_code"],
                device_id=body.device_id,
            )
        )
        add_event(
            conn,
            "order.created",
            order_id,
            {"paypal_order_id": paypal_order["id"], "amount": amount},
            resource_id=paypal_order["id"],
        )
    logger.info("order %s created as PayPal order %s", order_id, paypal_order["id"])
    return {
        "order_id": order_id,
        "paypal_order_id": paypal_order["id"],
        "status": "created",
        "amount": amount,
    }


# POST /payments/orders/{order_id}/authorize


def authorize_response(conn: Connection, order) -> dict:
    stored = last_event_payload(conn, order.id, "payment.authorized")
    return {
        "order_id": order.id,
        "paypal_order_id": order.paypal_order_id,
        "authorization_id": order.authorization_id,
        "authorization_status": stored.get("authorization_status", "CREATED"),
        "expires_at": iso_z(order.authorization_expires_at),
        "honor_period_ends_at": iso_z(order.honor_period_ends_at),
    }


@router.post("/orders/{order_id}/authorize")
async def authorize(order_id: str, request: Request, body: EmptyRequest | None = None) -> dict:
    engine: Engine = request.app.state.engine
    paypal = get_paypal(request)
    with engine.connect() as conn:
        order = load_order(conn, order_id)
        if order.authorization_id:
            return authorize_response(conn, order)
    if order.status != "checking":
        raise APIError(
            409,
            "invalid_state",
            f"Order {order_id} is {order.status}; authorize needs checking.",
            {"status": order.status},
        )

    paypal_order = await fetch_payer_details(engine, paypal, order_id)
    auth = first_authorization(paypal_order)
    if auth is None:
        # COMPLETED with an authorisation means an earlier call authorised but we didn't
        # store it; reuse that one rather than calling PayPal again.
        if paypal_order.get("status") != "APPROVED":
            raise APIError(
                409,
                "invalid_state",
                f"PayPal order is {paypal_order.get('status')}; authorize needs APPROVED.",
                {"paypal_status": paypal_order.get("status")},
            )
        try:
            result = await paypal.authorize_order(
                order.paypal_order_id, request_id=f"{order_id}:authorize"
            )
        except PayPalError as exc:
            raise paypal_error(engine, order_id, "authorize", exc) from None
        auth = first_authorization(result)
        if auth is None:
            raise APIError(
                502,
                "paypal_error",
                "PayPal authorize returned no authorization.",
                {"name": "NO_AUTHORIZATION", "debug_id": None, "issue": None},
            )

    authorized_at = parse_time(auth.get("create_time")) or utcnow()
    expires_at = parse_time(auth.get("expiration_time"))
    honor_ends = authorized_at + HONOR_PERIOD
    with engine.begin() as conn:
        stored = conn.execute(
            update(orders)
            .where(orders.c.id == order_id, orders.c.authorization_id.is_(None))
            .values(
                authorization_id=auth["id"],
                authorization_expires_at=expires_at,
                honor_period_ends_at=honor_ends,
            )
        ).rowcount
        if stored:
            add_event(
                conn,
                "payment.authorized",
                order_id,
                {
                    "authorization_id": auth["id"],
                    "authorization_status": auth.get("status"),
                    "expires_at": iso_z(expires_at),
                    "honor_period_ends_at": iso_z(honor_ends),
                },
                resource_id=auth["id"],
            )
        order = load_order(conn, order_id)
        return authorize_response(conn, order)


# POST /payments/authorizations/{authorization_id}/capture


def capture_response(conn: Connection, order) -> dict:
    stored = last_event_payload(conn, order.id, "payment.captured")
    return {
        "order_id": order.id,
        "authorization_id": order.authorization_id,
        "capture_id": order.capture_id,
        "capture_status": stored.get("capture_status", "COMPLETED"),
        "amount": stored.get("amount") or money(order.amount_value, order.currency_code),
        "status": order.status,
    }


def stored_decision(conn: Connection, order_id: str):
    return conn.execute(select(decisions).where(decisions.c.order_id == order_id)).first()


def resolve_decision(conn: Connection, order_id: str, resolution: str, note: str | None) -> None:
    conn.execute(
        update(decisions)
        .where(decisions.c.order_id == order_id, decisions.c.resolution.is_(None))
        .values(resolution=resolution, resolution_note=note, resolved_at=utcnow())
    )


@router.post("/authorizations/{authorization_id}/capture", status_code=201)
async def capture(authorization_id: str, body: CaptureRequest, request: Request) -> dict:
    engine: Engine = request.app.state.engine
    with engine.connect() as conn:
        order = order_by_authorization(conn, authorization_id)
        if order.capture_id:
            return capture_response(conn, order)
        decision = stored_decision(conn, order.id)
    if body.actor == "policy":
        allowed = order.status == "checking" and decision is not None
        allowed = allowed and decision.decision == "clean"
        need = "checking with a clean decision"
    else:
        allowed, need = order.status == "held", "held"
    if not allowed:
        raise APIError(
            409,
            "invalid_state",
            f"Order {order.id} is {order.status}; a {body.actor} capture needs {need}.",
            {"status": order.status, "actor": body.actor},
        )

    with engine.begin() as conn:
        add_event(
            conn,
            "capture.requested",
            order.id,
            {"authorization_id": authorization_id, "actor": body.actor, "note": body.note},
            resource_id=authorization_id,
        )
    try:
        result = await get_paypal(request).capture_authorization(
            authorization_id, request_id=f"{order.id}:capture"
        )
    except PayPalError as exc:
        raise paypal_error(engine, order.id, "capture", exc) from None

    amount = result.get("amount") or money(order.amount_value, order.currency_code)
    amount = {"currency_code": amount["currency_code"], "value": amount["value"]}
    with engine.begin() as conn:
        conn.execute(
            update(orders)
            .where(orders.c.id == order.id, orders.c.capture_id.is_(None))
            .values(capture_id=result["id"])
        )
        conn.execute(
            update(orders)
            .where(orders.c.id == order.id, orders.c.status.in_(("checking", "held")))
            .values(status="captured")
        )
        if body.actor == "reviewer":
            resolve_decision(conn, order.id, "capture", body.note)
        add_event(
            conn,
            "payment.captured",
            order.id,
            {
                "authorization_id": authorization_id,
                "capture_id": result["id"],
                "capture_status": result.get("status"),
                "amount": amount,
                "actor": body.actor,
                "note": body.note,
            },
            resource_id=result["id"],
        )
        order = load_order(conn, order.id)
        logger.info("order %s captured (%s) by %s", order.id, result.get("status"), body.actor)
        return capture_response(conn, order)


# POST /payments/authorizations/{authorization_id}/void


def void_response(order) -> dict:
    return {
        "order_id": order.id,
        "authorization_id": order.authorization_id,
        "authorization_status": "VOIDED",
        "status": "voided",
    }


@router.post("/authorizations/{authorization_id}/void")
async def void(authorization_id: str, body: VoidRequest, request: Request) -> dict:
    engine: Engine = request.app.state.engine
    with engine.connect() as conn:
        order = order_by_authorization(conn, authorization_id)
    if order.status == "voided":
        return void_response(order)
    if order.status != "held" or body.actor != "reviewer":
        raise APIError(
            409,
            "invalid_state",
            f"Order {order.id} is {order.status}; void needs held and a reviewer.",
            {"status": order.status, "actor": body.actor},
        )

    with engine.begin() as conn:
        add_event(
            conn,
            "void.requested",
            order.id,
            {"authorization_id": authorization_id, "actor": body.actor, "note": body.note},
            resource_id=authorization_id,
        )
    try:
        await get_paypal(request).void_authorization(
            authorization_id, request_id=f"{order.id}:void"
        )
    except PayPalError as exc:
        raise paypal_error(engine, order.id, "void", exc) from None

    with engine.begin() as conn:
        conn.execute(
            update(orders)
            .where(orders.c.id == order.id, orders.c.status == "held")
            .values(status="voided")
        )
        resolve_decision(conn, order.id, "void", body.note)
        add_event(
            conn,
            "payment.voided",
            order.id,
            {"authorization_id": authorization_id, "actor": body.actor, "note": body.note},
            resource_id=authorization_id,
        )
        order = load_order(conn, order.id)
    logger.info("order %s voided by reviewer", order.id)
    return void_response(order)
