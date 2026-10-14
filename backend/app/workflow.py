"""Starting the risk agent for an approved order (D9).

Hosted, it runs as a Render Workflows task run (`workflow/main.py`), which calls our
`POST /merchant/process` and retries it if it fails; the backend starts the run through Render's
API. Locally, or if Render's API refuses the call, it runs in this process after the response, as
in D4. Either way a `risk_agent.started` event names the runner.

Both triggers come here: the `CHECKOUT.ORDER.APPROVED` webhook and the storefront's `onApprove`
(`POST /merchant/start`). When both fire, two runs start and `/merchant/process` lets one claim
the order; the other gets 202 and waits for the decision.
"""

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import Settings
from app.db import events
from app.merchant import DECIDED, PAYPAL_READY, ProcessRequest, process_in_background
from app.payments import add_event, fetch_payer_details, load_order

logger = logging.getLogger("quietcheck.workflow")

router = APIRouter(prefix="/merchant", tags=["merchant"])


async def start_task_run(settings: Settings, order_id: str, idempotency_key: str) -> str:
    """Start the risk workflow task for one order (Render API `POST /task-runs`, 202); returns
    the task run ID. Render returns the first run for a repeated key within 24 hours."""
    async with httpx.AsyncClient(
        base_url=settings.render_api_url, transport=settings.render_transport, timeout=10.0
    ) as client:
        response = await client.post(
            "/task-runs",
            headers={"Authorization": f"Bearer {settings.render_api_key}"},
            json={
                "task": settings.risk_workflow_task,
                "input": [order_id],
                "idempotencyKey": idempotency_key,
            },
        )
        response.raise_for_status()
        return str(response.json()["id"])


# Task runs one order may get through reruns of a `failed` order, on top of its first run (each
# run already retries 3 times inside Render). Bounds what the public onApprove route can start.
MAX_RERUNS = 3


async def start_risk_agent(
    app_state, order_id: str, background: BackgroundTasks, rerun: bool = False
) -> str | None:
    """Start the risk agent on Render Workflows if configured, else in-process. Returns the
    runner (`render_workflow` or `in_process`), or None if the order is out of reruns.

    The webhook and `onApprove` share one task run per order (idempotency key = order ID). A
    rerun of a `failed` order is keyed `<order_id>:rerun:<n>`, n = the order's workflow runs so
    far, and stops after MAX_RERUNS: callers can't start unlimited runs.
    """
    settings: Settings = app_state.settings
    payload: dict = {"runner": "in_process"}
    if settings.render_api_key and settings.risk_workflow_task:
        key = order_id
        if rerun:
            with app_state.engine.connect() as conn:
                runs = len(_workflow_runs(conn, order_id))
            if runs > MAX_RERUNS:
                logger.warning("order %s: no reruns left (%d workflow runs)", order_id, runs)
                return None
            key = f"{order_id}:rerun:{runs}"
        try:
            run_id = await start_task_run(settings, order_id, key)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # The order must still be decided: fall back to running it here.
            logger.warning("order %s: workflow run not started (%s)", order_id, type(exc).__name__)
        else:
            payload = {"runner": "render_workflow", "task_run_id": run_id}
    if payload["runner"] == "in_process":
        background.add_task(process_in_background, app_state, order_id)
    with app_state.engine.begin() as conn:
        # Render returns the existing run for a repeated key: record each run once.
        if payload["runner"] == "in_process" or payload not in _workflow_runs(conn, order_id):
            add_event(conn, "risk_agent.started", order_id, payload)
    logger.info("order %s: risk agent started (%s)", order_id, payload["runner"])
    return payload["runner"]


def _workflow_runs(conn, order_id: str) -> list[dict]:
    """The order's recorded workflow task runs (`risk_agent.started` payloads)."""
    rows = conn.execute(
        select(events.c.payload).where(
            events.c.order_id == order_id, events.c.event_type == "risk_agent.started"
        )
    ).all()
    return [row.payload for row in rows if row.payload.get("runner") == "render_workflow"]


@router.post("/start")
async def start(body: ProcessRequest, request: Request, background: BackgroundTasks):
    """The storefront's `onApprove`: start the risk agent and return at once. 200 with the
    status if the order is already decided; otherwise 202, and the storefront polls the status.
    Nothing starts until PayPal reports the order approved."""
    with request.app.state.engine.connect() as conn:
        order = load_order(conn, body.order_id)
    status = order.status
    if status in DECIDED:
        return {"order_id": body.order_id, "status": status}
    if status != "checking":
        # Start only once PayPal has the buyer's approval. A premature call (anyone can reach
        # this through the storefront route) would otherwise use up the order's task run, whose
        # idempotency key then makes the real triggers reuse that finished run.
        paypal_order = await fetch_payer_details(
            request.app.state.engine, request.app.state.paypal, body.order_id
        )
        if paypal_order.get("status") in PAYPAL_READY:
            await start_risk_agent(
                request.app.state, body.order_id, background, rerun=status == "failed"
            )
    return JSONResponse(status_code=202, content={"order_id": body.order_id, "status": status})
