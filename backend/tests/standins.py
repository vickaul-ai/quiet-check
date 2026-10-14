"""Stand-ins for D4's /merchant/process and /merchant/decide, which own order status changes.

Until D4 exists, tests and the Gate A CLI write those rows directly, the way D2's tests insert
orders. `start_checking` is what /merchant/process does first; `record_decision` is what
/merchant/decide does before its PayPal action; `mark_held` is what it does after authorising a
hold. Nothing here calls PayPal.
"""

from sqlalchemy import Engine, insert, select, update

from app.db import checks, decisions, orders, utcnow
from app.ids import new_id

HIT_COUNTS = {"clean": 0, "hold": 1, "block": 2}
PAYPAL_ACTIONS = {"clean": "authorize_capture", "hold": "authorize", "block": "none"}


def start_checking(engine: Engine, order_id: str) -> None:
    with engine.begin() as conn:
        moved = conn.execute(
            update(orders)
            .where(orders.c.id == order_id, orders.c.status.in_(("created", "approved", "failed")))
            .values(status="checking")
        ).rowcount
    assert moved == 1, f"order {order_id} couldn't move to checking"


def record_decision(engine: Engine, order_id: str, decision: str, check_id: str | None = None):
    """Insert the decisions row (and, without a real check, a completed stand-in check)."""
    with engine.begin() as conn:
        if check_id is None:
            existing = conn.execute(
                select(checks.c.id).where(checks.c.order_id == order_id)
            ).first()
            check_id = existing.id if existing else new_id("chk")
            if not existing:
                conn.execute(
                    insert(checks).values(
                        id=check_id,
                        order_id=order_id,
                        status="completed",
                        hit_count=HIT_COUNTS[decision],
                        started_at=utcnow(),
                        completed_at=utcnow(),
                    )
                )
        decision_id = new_id("dec")
        conn.execute(
            insert(decisions).values(
                id=decision_id,
                order_id=order_id,
                check_id=check_id,
                decision=decision,
                rationale="Stand-in decision written before D4 exists.",
                paypal_action=PAYPAL_ACTIONS[decision],
            )
        )
    return decision_id


def mark_held(engine: Engine, order_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(orders)
            .where(orders.c.id == order_id, orders.c.status == "checking")
            .values(status="held")
        )
