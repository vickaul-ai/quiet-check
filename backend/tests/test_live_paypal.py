"""Live PayPal sandbox runs (D3/D4 acceptance). Skipped by default; run with

    cd backend && uv run pytest -m live -s

after `scripts/approve_orders.py` has saved at least two approved orders, with `make
dev-backend` stopped and `ngrok http 8000` running. The module starts Members A-C with
synthetic lists, putting the second order's device ID on Member B's list, serves the backend on
:8000, and runs the real risk agent on both orders: the first is clean (authorise + capture),
the second a hold (authorise, then a reviewer void). Then it waits for PayPal's webhooks.
CI runs the same flows against the fake PayPal (test_payments.py, test_merchant.py).
"""

import httpx
import pytest
from sqlalchemy import select

from app.config import VAR_DIR
from app.db import make_engine, orders
from app.identifiers import identifier_hash
from tests import live

pytestmark = pytest.mark.live

# PayPal's shipping.address (address_portable) names that contracts.md's normalisation reads.
ADDRESS_FIELDS = {"address_line_1", "admin_area_2", "admin_area_1", "postal_code", "country_code"}
LIVE_TEST_DIR = VAR_DIR / "live" / "pytest"


@pytest.fixture(scope="module")
def server():
    settings = live.live_settings()
    if not (settings.paypal_client_id and settings.paypal_client_secret):
        pytest.skip("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
    if len(live.load_saved()) < 2:
        pytest.skip("need 2 saved approved orders: run scripts/approve_orders.py first")
    clean, hold = live.take_order(), live.take_order()
    engine = make_engine(settings.database_url)
    with engine.connect() as conn:
        device = conn.execute(
            select(orders.c.device_id).where(orders.c.id == hold["order_id"])
        ).scalar()
    engine.dispose()
    seeded = {"B": [identifier_hash("device_id", device)]}
    with live.consortium(LIVE_TEST_DIR / "members", LIVE_TEST_DIR / "logs", seeded) as overrides:
        with live.serve(live.live_settings(**overrides)) as app:
            with httpx.Client(base_url=live.BASE_URL, timeout=120) as client:
                yield app, client, {"clean": clean, "hold": hold}


def process(client, order_id: str, status: str) -> dict:
    response = client.post("/merchant/process", json={"order_id": order_id})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    detail = client.get(f"/merchant/orders/{order_id}").json()
    assert detail["payer_email"], "PayPal returned no payer email"
    assert ADDRESS_FIELDS <= set(detail["shipping_address"]), sorted(detail["shipping_address"])
    assert len(detail["receipts"]) == 9 and all(r["verified"] for r in detail["receipts"])
    assert detail["authorization_id"] and detail["honor_period_ends_at"]
    print(f"\n{order_id}: {detail['decision']['rationale_source']} rationale")
    return detail


def assert_webhooks(app, order_id: str, expected: set[str]) -> None:
    seen = live.wait_for_webhooks(app.state.engine, order_id, expected)
    print(f"\n{order_id} webhooks: {seen}")
    assert set(seen) == expected, f"missing: {sorted(expected - set(seen))}"
    assert all(seen.values()), f"unverified: {[t for t, ok in seen.items() if not ok]}"


def test_live_clean_authorize_then_capture(server) -> None:
    app, client, saved = server
    order_id = saved["clean"]["order_id"]
    detail = process(client, order_id, "captured")
    assert detail["capture_id"] and detail["hit_count"] == 0
    assert_webhooks(
        app,
        order_id,
        {"CHECKOUT.ORDER.APPROVED", "PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"},
    )


def test_live_hold_authorize_then_void(server) -> None:
    app, client, saved = server
    order_id = saved["hold"]["order_id"]
    detail = process(client, order_id, "held")
    assert detail["hit_count"] == 1
    response = client.post(
        f"/payments/authorizations/{detail['authorization_id']}/void",
        json={"actor": "reviewer", "note": "D4 live test"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "voided"
    assert_webhooks(
        app,
        order_id,
        {
            "CHECKOUT.ORDER.APPROVED",
            "PAYMENT.AUTHORIZATION.CREATED",
            "PAYMENT.AUTHORIZATION.VOIDED",
        },
    )
