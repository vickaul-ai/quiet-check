"""Quiet Check risk workflow (Render Workflows, D9).

The backend starts `process_order` for each approved order (`app/workflow.py`). The task runs the
merchant risk agent by calling the backend over Render's private network, so each order's
check -> decide -> PayPal run is a durable task run with retries and its own logs in Render:

- `run_risk_agent` calls `POST /merchant/process`, which runs the AP3 check, the policy, the
  rationale and the PayPal action. A 5xx (a member down, a PayPal error) raises, so Render retries
  it; the backend's rerun reuses the stored check and decision and resumes the PayPal action (D4).
- If another run already holds the order (202), `wait_for_decision` polls the status endpoint.

Env: BACKEND_HOST (the backend's internal hostname; the Blueprint sets it) or BACKEND_URL, and
BACKEND_API_TOKEN. Start command: `python main.py`.
"""

import asyncio
import logging
import os

import httpx
from render import Retry, TaskContext, Workflows

app = Workflows()
# Every line names the order ("order <id>: ..."), as the backend's and web's lines do, so one
# Render log search shows an order's whole path (D10).
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("quietcheck.workflow")

DECIDED = ("captured", "held", "blocked", "voided")
POLL_SECONDS = 2
WAIT_SECONDS = 90


def backend_url() -> str:
    host = os.environ.get("BACKEND_HOST")
    # Render routes private traffic on port 10000 to a web service's main HTTP server.
    url = os.environ.get("BACKEND_URL") or (f"http://{host}:10000" if host else "")
    if not url:
        raise RuntimeError("set BACKEND_HOST or BACKEND_URL")
    return url.rstrip("/")


def backend() -> httpx.AsyncClient:
    token = os.environ.get("BACKEND_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(base_url=backend_url(), headers=headers, timeout=90.0)


class BackendFailed(Exception):
    """The backend failed (5xx) or refused us (401): the task fails, and Render retries it."""


@app.task(timeout_seconds=180, retry=Retry(max_retries=3, wait_duration_ms=3000))
async def run_risk_agent(ctx: TaskContext, order_id: str) -> dict:
    logger.info("order %s: calling /merchant/process", order_id)
    async with backend() as client:
        response = await client.post("/merchant/process", json={"order_id": order_id})
    body = response.json() if response.content else {}
    code = body.get("error", {}).get("code", "error")
    if response.status_code in (404, 409, 422):
        # Unknown order, or not processable (e.g. PayPal hasn't approved it): retrying won't help.
        logger.warning("order %s: not processable (%s %s)", order_id, response.status_code, code)
        return {"order_id": order_id, "error": code}
    if response.status_code >= 400:
        # 5xx (a member down, a PayPal error) is retried; 401 (a wrong token) fails visibly.
        logger.warning(
            "order %s: backend %s %s; failing the attempt", order_id, response.status_code, code
        )
        raise BackendFailed(f"order {order_id}: {response.status_code} {code}")
    logger.info("order %s: %s (%s)", order_id, body.get("status"), response.status_code)
    return {"order_id": order_id, "status": body.get("status"), "decision": body.get("decision")}


@app.task(timeout_seconds=WAIT_SECONDS + 30, retry=Retry(max_retries=0, wait_duration_ms=1000))
async def wait_for_decision(ctx: TaskContext, order_id: str) -> dict:
    status = "checking"
    async with backend() as client:
        for _ in range(WAIT_SECONDS // POLL_SECONDS):
            response = await client.get(f"/merchant/orders/{order_id}/status")
            if response.status_code == 200:
                status = response.json()["status"]
                if status in DECIDED or status == "failed":
                    break
            await asyncio.sleep(POLL_SECONDS)
    logger.info("order %s: waited for another run; status %s", order_id, status)
    return {"order_id": order_id, "status": status}


# Long enough for run_risk_agent's worst case (4 attempts x 180 s plus backoff) and the wait.
@app.task(timeout_seconds=1080, retry=Retry(max_retries=0, wait_duration_ms=1000))
async def process_order(ctx: TaskContext, order_id: str) -> dict:
    """Entry point: one run per approved order."""
    result = await ctx.run(run_risk_agent, order_id)
    if result.get("status") == "checking":  # another run holds the order
        result = await ctx.run(wait_for_decision, order_id)
    return result


if __name__ == "__main__":
    app.start()
