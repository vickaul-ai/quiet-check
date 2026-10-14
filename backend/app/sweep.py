"""Recover orders stuck in `checking` (D9 follow-up).

A run that dies mid-way (the backend restarts or redeploys during a check) leaves its order
`checking`, and nothing reran it (known since D4). Every `sweep_interval_s` the backend looks for
orders that have been `checking` for longer than `sweep_after_s`, well past the longest real run
(60 s check timeout + 10 s rationale + PayPal calls), moves each to `failed` with one conditional
update, and reruns the risk agent in-process. A rerun resumes safely (ADR 0008): it reuses a
completed check and a stored decision. Each order gets at most MAX_SWEEPS, recorded as
`risk_agent.started` events with `reason: sweep`.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db import events, orders
from app.merchant import process_in_background
from app.payments import add_event

logger = logging.getLogger("quietcheck.sweep")

MAX_SWEEPS = 2


def _sweeps(conn, order_id: str) -> int:
    rows = conn.execute(
        select(events.c.payload).where(
            events.c.order_id == order_id, events.c.event_type == "risk_agent.started"
        )
    ).all()
    return sum(1 for row in rows if row.payload.get("reason") == "sweep")


async def sweep_once(app_state) -> list[str]:
    """Rerun every order stuck in `checking`; returns their IDs."""
    settings = app_state.settings
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.sweep_after_s)
    engine = app_state.engine
    with engine.connect() as conn:
        stuck = [
            row.id
            for row in conn.execute(
                select(orders.c.id).where(
                    orders.c.status == "checking", orders.c.updated_at < cutoff
                )
            ).all()
        ]
    swept = []
    for order_id in stuck:
        with engine.begin() as conn:
            if _sweeps(conn, order_id) >= MAX_SWEEPS:
                continue
            # Conditional: a run that finished meanwhile has moved the order on.
            moved = conn.execute(
                update(orders)
                .where(orders.c.id == order_id, orders.c.status == "checking")
                .values(status="failed")
            ).rowcount
            if not moved:
                continue
            add_event(
                conn, "risk_agent.started", order_id, {"runner": "in_process", "reason": "sweep"}
            )
        logger.warning("order %s: stuck in checking; rerunning", order_id)
        await process_in_background(app_state, order_id)
        swept.append(order_id)
    return swept


async def sweep_forever(app_state) -> None:
    while True:
        await asyncio.sleep(app_state.settings.sweep_interval_s)
        try:
            await sweep_once(app_state)
        except Exception:
            logger.exception("sweep failed")
