"""Live PayPal sandbox runs (D3 acceptance). Skipped by default; run with

    cd backend && uv run pytest -m live -s

after `scripts/approve_orders.py` has saved at least two approved orders, with `make
dev-backend` stopped and `ngrok http 8000` running. Each test serves the backend on :8000,
takes one saved order, plays D4's part with the stand-ins, and waits for PayPal's webhooks.
CI runs the same flows against the fake PayPal (test_payments.py, test_webhooks.py).
"""

import httpx
import pytest
from sqlalchemy import select

from app.db import orders
from tests import live
from tests.standins import mark_held, record_decision, start_checking

pytestmark = pytest.mark.live

# PayPal's shipping.address (address_portable) names that contracts.md's normalisation reads.
ADDRESS_FIELDS = {"address_line_1", "admin_area_2", "admin_area_1", "postal_code", "country_code"}


@pytest.fixture(scope="module")
def server():
    settings = live.live_settings()
    if not (settings.paypal_client_id and settings.paypal_client_secret):
        pytest.skip("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
    if len(live.load_saved()) < 2:
        pytest.skip("need 2 saved approved orders: run scripts/approve_orders.py first")
    with live.serve(settings) as app:
        with httpx.Client(base_url=live.BASE_URL, timeout=60) as client:
            yield app, client


def authorize_saved_order(app, client, decision: str) -> tuple[dict, dict]:
    saved = live.take_order()
    assert saved, "no saved order left"
    order_id = saved["order_id"]
    start_checking(app.state.engine, order_id)
    record_decision(app.state.engine, order_id, decision)
    response = client.post(f"/payments/orders/{order_id}/authorize", json={})
    assert response.status_code == 200, response.text
    auth = response.json()
    assert auth["authorization_status"] == "CREATED"
    assert auth["expires_at"] and auth["honor_period_ends_at"]

    with app.state.engine.connect() as conn:
        row = conn.execute(select(orders).where(orders.c.id == order_id)).first()
    assert row.payer_email, "PayPal returned no payer email"
    assert ADDRESS_FIELDS <= set(row.shipping_address), sorted(row.shipping_address)
    return saved, auth


def assert_webhooks(app, order_id: str, expected: set[str]) -> None:
    seen = live.wait_for_webhooks(app.state.engine, order_id, expected)
    print(f"\n{order_id} webhooks: {seen}")
    assert set(seen) == expected, f"missing: {sorted(expected - set(seen))}"
    assert all(seen.values()), f"unverified: {[t for t, ok in seen.items() if not ok]}"


def test_live_authorize_then_capture(server) -> None:
    app, client = server
    saved, auth = authorize_saved_order(app, client, "clean")
    response = client.post(
        f"/payments/authorizations/{auth['authorization_id']}/capture", json={"actor": "policy"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["capture_status"]) == ("captured", "COMPLETED")
    assert_webhooks(
        app,
        saved["order_id"],
        {"CHECKOUT.ORDER.APPROVED", "PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"},
    )


def test_live_authorize_then_void(server) -> None:
    app, client = server
    saved, auth = authorize_saved_order(app, client, "hold")
    mark_held(app.state.engine, saved["order_id"])
    response = client.post(
        f"/payments/authorizations/{auth['authorization_id']}/void",
        json={"actor": "reviewer", "note": "D3 live test"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "voided"
    assert_webhooks(
        app,
        saved["order_id"],
        {
            "CHECKOUT.ORDER.APPROVED",
            "PAYMENT.AUTHORIZATION.CREATED",
            "PAYMENT.AUTHORIZATION.VOIDED",
        },
    )
