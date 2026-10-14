"""/catalog and /payments against the fake PayPal (CI runs the same flows as the live tests)."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.db import decisions, events, orders
from app.paypal import PayPalClient
from tests.fake_paypal import _error
from tests.stored_checks import store_check

ADDRESS = {
    "address_line_1": "1 Main St",
    "admin_area_2": "San Jose",
    "admin_area_1": "CA",
    "postal_code": "95131",
    "country_code": "US",
}


def rows(api, table, **where):
    query = select(table)
    for column, value in where.items():
        query = query.where(getattr(table.c, column) == value)
    with api.app.state.engine.connect() as conn:
        return conn.execute(query).all()


def create(api, items=None, device_id="dev-test-1") -> dict:
    items = items or [{"sku": "backpack-blue-m", "quantity": 1}]
    response = api.post("/payments/orders", json={"items": items, "device_id": device_id})
    assert response.status_code == 201, response.text
    return response.json()


def processed_order(api, fake, decision: str, approve: bool = True) -> dict:
    """create -> buyer approves -> stored check -> POST /merchant/process (the real risk agent:
    policy, rationale, decide and its PayPal action)."""
    order = create(api)
    if approve:
        fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
    store_check(api.app.state.engine, order["order_id"], decision)
    response = api.post("/merchant/process", json={"order_id": order["order_id"]})
    assert response.status_code == 200, response.text
    return {**order, "process": response.json()}


def authorize(api, order_id: str) -> dict:
    response = api.post(f"/payments/orders/{order_id}/authorize", json={})
    assert response.status_code == 200, response.text
    return response.json()


def event_types(api, order_id: str) -> list[str]:
    return [r.event_type for r in sorted(rows(api, events, order_id=order_id), key=lambda r: r.id)]


# Catalog


def test_catalog_lists_every_sku(api) -> None:
    items = api.get("/catalog").json()["items"]
    assert [i["sku"] for i in items] == [
        "backpack-blue-s",
        "backpack-blue-m",
        "backpack-blue-l",
        "backpack-black-m",
        "duffel-green",
        "bottle-steel",
        "cap-navy",
    ]
    assert items[1] == {
        "sku": "backpack-blue-m",
        "name": "Trail Backpack, Blue, Medium",
        "unit_amount": {"currency_code": "USD", "value": "49.00"},
    }


# Create order


def test_create_prices_from_catalog_and_sends_order_id(api, fake) -> None:
    body = create(
        api,
        [{"sku": "backpack-blue-m", "quantity": 2}, {"sku": "cap-navy", "quantity": 1}],
    )
    assert body["order_id"].startswith("ord_")
    assert body["status"] == "created"
    assert body["amount"] == {"currency_code": "USD", "value": "117.00"}

    (call,) = fake.calls("POST", "/v2/checkout/orders")
    assert call.headers["paypal-request-id"] == f"{body['order_id']}:create"
    sent = json.loads(call.content)
    assert sent["intent"] == "AUTHORIZE"
    unit = sent["purchase_units"][0]
    assert unit["custom_id"] == body["order_id"]
    assert unit["amount"]["value"] == "117.00"
    assert unit["amount"]["breakdown"]["item_total"]["value"] == "117.00"
    assert [(i["sku"], i["quantity"], i["unit_amount"]["value"]) for i in unit["items"]] == [
        ("backpack-blue-m", "2", "49.00"),
        ("cap-navy", "1", "19.00"),
    ]

    (row,) = rows(api, orders, id=body["order_id"])
    assert row.paypal_order_id == body["paypal_order_id"]
    assert row.device_id == "dev-test-1"
    assert row.amount_value == "117.00"
    assert row.items[0]["name"] == "Trail Backpack, Blue, Medium"
    assert event_types(api, body["order_id"]) == ["order.created"]


def test_create_rejects_agent_prices_unknown_skus_and_bad_bodies(api, fake) -> None:
    def post(body):
        return api.post("/payments/orders", json=body)

    cap = {"sku": "cap-navy", "quantity": 1}
    assert post({"items": [{**cap, "unit_amount": "0.01"}], "device_id": "d"}).status_code == 422
    unknown = post({"items": [{"sku": "nope", "quantity": 1}], "device_id": "d"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    assert post({"items": [], "device_id": "d"}).status_code == 422
    assert post({"items": [{**cap, "quantity": 0}], "device_id": "d"}).status_code == 422
    assert post({"items": [cap]}).status_code == 422
    dupes = [cap, cap]
    assert post({"items": dupes, "device_id": "d"}).status_code == 422
    assert fake.calls("POST", "/v2/checkout/orders") == []


def test_each_create_is_a_new_order(api) -> None:
    assert create(api)["order_id"] != create(api)["order_id"]


def test_paypal_error_is_502_with_paypal_details(api, fake) -> None:
    fake.fail_next["/v2/checkout/orders"] = _error(500, "INTERNAL_SERVER_ERROR")
    response = api.post(
        "/payments/orders", json={"items": [{"sku": "cap-navy", "quantity": 1}], "device_id": "d"}
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "paypal_error"
    assert error["details"]["name"] == "INTERNAL_SERVER_ERROR"
    assert error["details"]["debug_id"].startswith("fakedebug")


def test_token_is_cached_and_refreshed_after_401(api, fake) -> None:
    create(api)
    create(api)
    assert fake.token_calls == 1
    fake.expire_tokens = True
    create(api)
    assert fake.token_calls == 2


# Authorise -> capture (clean) and authorise -> void (hold)


def test_clean_flow_authorize_then_policy_capture(api, fake) -> None:
    order = processed_order(api, fake, "clean")
    order_id = order["order_id"]
    assert order["process"]["status"] == "captured"

    auth = authorize(api, order_id)  # a repeat: the stored authorisation
    assert auth["paypal_order_id"] == order["paypal_order_id"]
    assert auth["authorization_status"] == "CREATED"
    expires = datetime.fromisoformat(auth["expires_at"])
    honor = datetime.fromisoformat(auth["honor_period_ends_at"])
    assert timedelta(days=25) < expires - honor < timedelta(days=27)  # 29 days vs 3 days
    (call,) = fake.calls("POST", "/authorize")
    assert call.headers["paypal-request-id"] == f"{order_id}:authorize"

    (row,) = rows(api, orders, id=order_id)
    assert row.status == "captured"
    assert row.payer_email == "ana@example.com"
    assert row.shipping_address == ADDRESS
    assert row.authorization_id == auth["authorization_id"]
    (call,) = fake.calls("POST", "/capture")
    assert call.headers["paypal-request-id"] == f"{order_id}:capture"
    assert json.loads(call.content) == {"final_capture": True}

    # Repeats return the stored result without calling PayPal.
    again = api.post(
        f"/payments/authorizations/{auth['authorization_id']}/capture", json={"actor": "policy"}
    )
    assert again.status_code == 201
    captured = again.json()
    assert captured["status"] == "captured"
    assert captured["capture_status"] == "COMPLETED"
    assert captured["amount"] == {"currency_code": "USD", "value": "49.00"}
    assert captured["capture_id"] == row.capture_id
    assert len(fake.calls("POST", "/authorize")) == 1
    assert len(fake.calls("POST", "/capture")) == 1
    assert event_types(api, order_id) == [
        "order.created",
        "decision.made",
        "payment.authorized",
        "capture.requested",
        "payment.captured",
    ]
    (decision,) = rows(api, decisions, order_id=order_id)
    assert decision.resolution is None  # policy captures aren't reviewer resolutions


def test_hold_flow_authorize_then_reviewer_void(api, fake) -> None:
    order = processed_order(api, fake, "hold")
    order_id = order["order_id"]
    assert order["process"]["status"] == "held"
    auth_id = authorize(api, order_id)["authorization_id"]

    response = api.post(
        f"/payments/authorizations/{auth_id}/void",
        json={"actor": "reviewer", "note": "Buyer unreachable"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "order_id": order_id,
        "authorization_id": auth_id,
        "authorization_status": "VOIDED",
        "status": "voided",
    }
    (call,) = fake.calls("POST", "/void")
    assert call.headers["paypal-request-id"] == f"{order_id}:void"
    (decision,) = rows(api, decisions, order_id=order_id)
    assert (decision.resolution, decision.resolution_note) == ("void", "Buyer unreachable")
    assert decision.resolved_at is not None

    again = api.post(f"/payments/authorizations/{auth_id}/void", json={"actor": "reviewer"})
    assert again.status_code == 200
    assert len(fake.calls("POST", "/void")) == 1
    late = api.post(f"/payments/authorizations/{auth_id}/capture", json={"actor": "reviewer"})
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "invalid_state"
    voided = [r for r in rows(api, events, order_id=order_id) if r.event_type == "payment.voided"]
    assert voided[0].payload["note"] == "Buyer unreachable"


def test_hold_flow_reviewer_capture(api, fake) -> None:
    order = processed_order(api, fake, "hold")
    auth = authorize(api, order["order_id"])
    response = api.post(
        f"/payments/authorizations/{auth['authorization_id']}/capture",
        json={"actor": "reviewer", "note": "Confirmed with buyer by phone"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "captured"
    (decision,) = rows(api, decisions, order_id=order["order_id"])
    assert decision.resolution == "capture"
    void = api.post(
        f"/payments/authorizations/{auth['authorization_id']}/void", json={"actor": "reviewer"}
    )
    assert void.status_code == 409


# Status guards


def test_authorize_needs_checking_and_paypal_approved(api, fake) -> None:
    order = create(api)
    response = api.post(f"/payments/orders/{order['order_id']}/authorize", json={})
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"status": "created"}

    # Force `checking` without approval (the risk agent itself won't start an unapproved order).
    with api.app.state.engine.begin() as conn:
        conn.execute(
            update(orders).where(orders.c.id == order["order_id"]).values(status="checking")
        )
    response = api.post(f"/payments/orders/{order['order_id']}/authorize", json={})
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"paypal_status": "CREATED"}
    assert fake.calls("POST", "/authorize") == []
    assert api.post("/payments/orders/ord_missing/authorize", json={}).status_code == 404
    extra = api.post(f"/payments/orders/{order['order_id']}/authorize", json={"amount": "1"})
    assert extra.status_code == 422


def test_capture_and_void_guards(api, fake) -> None:
    held = processed_order(api, fake, "hold")
    held_auth = authorize(api, held["order_id"])["authorization_id"]
    captured = processed_order(api, fake, "clean")
    captured_auth = authorize(api, captured["order_id"])["authorization_id"]

    def call(auth_id, action, actor):
        return api.post(f"/payments/authorizations/{auth_id}/{action}", json={"actor": actor})

    # held: only a reviewer may move money
    assert call(held_auth, "capture", "policy").status_code == 409
    assert call(held_auth, "void", "policy").status_code == 409
    # captured: nothing left to void
    assert call(captured_auth, "void", "reviewer").status_code == 409
    missing = api.post("/payments/authorizations/NOPE/void", json={"actor": "reviewer"})
    assert missing.status_code == 404
    assert call(held_auth, "capture", "robot").status_code == 422
    assert len(fake.calls("POST", "/capture")) == 1  # the clean order's policy capture
    assert fake.calls("POST", "/void") == []


def test_capture_paypal_error_leaves_order_held(api, fake) -> None:
    order = processed_order(api, fake, "hold")
    auth_id = authorize(api, order["order_id"])["authorization_id"]
    fake.fail_next["/capture"] = _error(422, "UNPROCESSABLE_ENTITY", "AUTHORIZATION_EXPIRED")
    response = api.post(f"/payments/authorizations/{auth_id}/capture", json={"actor": "reviewer"})
    assert response.status_code == 502
    assert response.json()["error"]["details"]["issue"] == "AUTHORIZATION_EXPIRED"
    (row,) = rows(api, orders, id=order["order_id"])
    assert row.status == "held" and row.capture_id is None
    assert event_types(api, order["order_id"])[-1] == "paypal.error"


def test_authorize_reuses_an_authorization_paypal_already_made(api, fake) -> None:
    """A crash after PayPal authorised but before we stored it: the retry reads it back."""
    order = create(api)
    fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
    paypal_client = PayPalClient(
        "https://api-m.sandbox.paypal.com", "fake-id", "fake-secret", transport=fake.transport
    )
    api.portal.call(paypal_client.authorize_order, order["paypal_order_id"], "elsewhere")
    store_check(api.app.state.engine, order["order_id"], "clean")
    response = api.post("/merchant/process", json={"order_id": order["order_id"]})
    assert response.status_code == 200, response.text
    auth = authorize(api, order["order_id"])
    assert auth["authorization_id"] in fake.authorizations
    assert len(fake.calls("POST", "/authorize")) == 1


def test_reauthorize_client_method(api, fake) -> None:
    order = processed_order(api, fake, "hold")
    auth_id = authorize(api, order["order_id"])["authorization_id"]
    paypal_client = api.app.state.paypal
    new = api.portal.call(paypal_client.reauthorize_authorization, auth_id, "x:reauthorize")
    assert new["id"] != auth_id and new["status"] == "CREATED"
    (call,) = fake.calls("POST", "/reauthorize")
    assert call.headers["paypal-request-id"] == "x:reauthorize"
