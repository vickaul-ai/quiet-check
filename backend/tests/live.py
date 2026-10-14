"""Shared pieces for the live sandbox runs: `scripts/approve_orders.py`, `pytest -m live` and
`scripts/gate_a.py`.

All three serve the backend on :8000 (QC_LIVE_PORT) so PayPal's webhooks reach it through
`ngrok http 8000`, and share one SQLite DB plus the saved approved orders in
`backend/var/live/` (git-ignored). Each saved order can be authorised once, so it is removed
from the file when a run takes it. One third-party guide says unapproved orders expire after
3 hours; PayPal's docs don't confirm a figure, so use saved orders soon after approval.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from sqlalchemy import Engine, select

from app.config import VAR_DIR, Settings
from app.db import events
from app.main import create_app

LIVE_DIR = VAR_DIR / "live"
LIVE_DB = LIVE_DIR / "live.db"
SAVED_ORDERS = LIVE_DIR / "approved_orders.json"
PORT = int(os.environ.get("QC_LIVE_PORT") or 8000)
BASE_URL = f"http://127.0.0.1:{PORT}"
WEBHOOK_TIMEOUT_S = float(os.environ.get("QC_LIVE_WEBHOOK_TIMEOUT") or 180)


def live_settings(**overrides) -> Settings:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    overrides = {"database_url": f"sqlite:///{LIVE_DB}", "start_initiator": False} | overrides
    return Settings.from_env(**overrides)


def log_to(path: Path) -> None:
    """Send every log line to one file (not the terminal), as the test run does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


@contextmanager
def serve(settings: Settings, port: int = PORT) -> Iterator:
    """Run the backend with uvicorn in a background thread; yields the app."""
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(
                f"backend didn't start on :{port}. Is `make dev-backend` still running?"
            )
        time.sleep(0.05)
    try:
        yield app
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def ngrok_url(port: int = PORT) -> str | None:
    """The public URL of an ngrok tunnel to this port, from ngrok's local API, if any."""
    try:
        tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=2).json()["tunnels"]
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    for tunnel in tunnels:
        if tunnel.get("config", {}).get("addr", "").endswith(f":{port}"):
            return tunnel.get("public_url")
    return None


def load_saved() -> list[dict]:
    return json.loads(SAVED_ORDERS.read_text()) if SAVED_ORDERS.exists() else []


def save_order(order_id: str, paypal_order_id: str) -> None:
    saved = load_saved()
    saved.append(
        {
            "order_id": order_id,
            "paypal_order_id": paypal_order_id,
            "approved_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    SAVED_ORDERS.write_text(json.dumps(saved, indent=2) + "\n")


def take_order() -> dict | None:
    """Remove and return the oldest saved order (each can be authorised only once)."""
    saved = load_saved()
    if not saved:
        return None
    SAVED_ORDERS.write_text(json.dumps(saved[1:], indent=2) + "\n")
    return saved[0]


def wait_for_webhooks(
    engine: Engine, order_id: str, event_types: set[str], timeout_s: float = WEBHOOK_TIMEOUT_S
) -> dict[str, bool]:
    """Wait until each event type has arrived for the order; returns {type: verified}."""
    deadline = time.monotonic() + timeout_s
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                select(events.c.event_type, events.c.verified).where(
                    events.c.order_id == order_id, events.c.source == "paypal_webhook"
                )
            ).all()
        seen = {r.event_type: bool(r.verified) for r in rows if r.event_type in event_types}
        if set(seen) == event_types or time.monotonic() > deadline:
            return seen
        time.sleep(2)
