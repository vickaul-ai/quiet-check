"""POST /webhooks/paypal against the fake PayPal's verify-webhook-signature."""

import json

from sqlalchemy import select

from app.db import events, orders
from app.payments import iso_z
from tests.fake_paypal import webhook_event, webhook_headers
from tests.stored_checks import store_check
from tests.test_payments import ADDRESS, authorize, create, processed_order, rows


def send(api, event: dict, signature: str = "good", raw: bytes | None = None):
    content = raw if raw is not None else json.dumps(event).encode()
    return api.post("/webhooks/paypal", content=content, headers=webhook_headers(signature))


def order_row(api, order_id):
    (row,) = rows(api, orders, id=order_id)
    return row


def webhook_rows(api):
    with api.app.state.engine.connect() as conn:
        return conn.execute(select(events).where(events.c.source == "paypal_webhook")).all()


def test_approved_moves_created_to_approved_and_dedupes(api, fake) -> None:
    order = create(api)
    event = webhook_event(
        "CHECKOUT.ORDER.APPROVED", {"id": order["paypal_order_id"], "status": "APPROVED"}
    )
    response = send(api, event)
    assert response.status_code == 200 and response.json() == {"received": True}
    assert order_row(api, order["order_id"]).status == "approved"

    (row,) = webhook_rows(api)
    assert row.verified is True
    assert row.paypal_event_id == event["id"]
    assert row.order_id == order["order_id"]
    assert row.resource_id == order["paypal_order_id"]
    assert row.payload == event

    assert send(api, event).status_code == 200  # duplicate delivery
    assert len(webhook_rows(api)) == 1


def test_verification_sends_raw_event_and_webhook_id(api, fake) -> None:
    raw = b'{"id":"WH-RAW-1","event_type":"CUSTOMER.DISPUTE.CREATED","resource":{"x":1.10}}'
    assert send(api, {}, raw=raw).status_code == 200
    (call,) = fake.calls("POST", "/verify-webhook-signature")
    body = call.content.decode()
    assert body.endswith('"webhook_event": ' + raw.decode() + "}")  # byte-for-byte
    sent = json.loads(body)
    assert sent["webhook_id"] == "WH-FAKE-0001"
    assert sent["transmission_sig"] == "good"


def test_unverified_event_is_stored_without_its_id_and_rejected(api, fake) -> None:
    order = create(api)
    event = webhook_event("CHECKOUT.ORDER.APPROVED", {"id": order["paypal_order_id"]})
    response = send(api, event, signature="forged")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "webhook_unverified"
    (row,) = webhook_rows(api)
    assert row.verified is False
    assert row.paypal_event_id is None  # can't block the real event's ID
    assert row.order_id is None
    assert row.resource_id is None
    assert row.payload == {"claimed_id": event["id"], "event_type": "CHECKOUT.ORDER.APPROVED"}
    assert order_row(api, order["order_id"]).status == "created"

    assert send(api, event).status_code == 200  # the genuine delivery still acts
    assert order_row(api, order["order_id"]).status == "approved"


def test_missing_headers_and_bad_bodies(api, fake) -> None:
    event = webhook_event("CHECKOUT.ORDER.APPROVED", {"id": "X"})
    response = api.post("/webhooks/paypal", json=event)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "webhook_unverified"
    assert fake.calls("POST", "/verify-webhook-signature") == []  # no headers, no call
    assert send(api, {}, raw=b"not json").json()["error"]["code"] == "bad_request"
    assert send(api, {}, raw=b'{"id": "x"}').json()["error"]["code"] == "bad_request"


def held_order(api, fake) -> tuple[dict, str]:
    order = processed_order(api, fake, "hold")
    return order, authorize(api, order["order_id"])["authorization_id"]


def test_approved_webhook_runs_the_risk_agent(api, fake, model) -> None:
    """CHECKOUT.ORDER.APPROVED alone takes the order to a decision (after the 200)."""
    order = create(api)
    fake.approve(order["paypal_order_id"], "ben@example.com", ADDRESS)
    store_check(api.app.state.engine, order["order_id"], "hold")
    event = webhook_event("CHECKOUT.ORDER.APPROVED", {"id": order["paypal_order_id"]})
    assert send(api, event).status_code == 200
    row = order_row(api, order["order_id"])
    assert row.status == "held" and row.authorization_id
    assert len(model.calls) == 1
    assert send(api, event).status_code == 200  # a duplicate doesn't run it again
    assert len(model.calls) == 1
    assert len(fake.calls("POST", "/authorize")) == 1


def test_approved_webhook_for_an_unapproved_paypal_order_leaves_it_approved(api, fake) -> None:
    order = create(api)  # PayPal still says CREATED
    event = webhook_event("CHECKOUT.ORDER.APPROVED", {"id": order["paypal_order_id"]})
    assert send(api, event).status_code == 200
    assert order_row(api, order["order_id"]).status == "approved"
    assert fake.calls("POST", "/authorize") == []


def test_authorization_created_fills_missing_fields_only(api, fake) -> None:
    order = create(api)
    resource = {
        "id": "AUTH-FROM-WEBHOOK",
        "status": "CREATED",
        "custom_id": order["order_id"],
        "create_time": "2026-10-20T14:03:11Z",
        "expiration_time": "2026-11-18T14:03:11Z",
    }
    assert send(api, webhook_event("PAYMENT.AUTHORIZATION.CREATED", resource)).status_code == 200
    row = order_row(api, order["order_id"])
    assert row.authorization_id == "AUTH-FROM-WEBHOOK"
    assert iso_z(row.honor_period_ends_at) == "2026-10-23T14:03:11Z"  # any DB time zone
    assert row.status == "created"  # no status change

    other = {**resource, "id": "AUTH-OTHER"}
    send(api, webhook_event("PAYMENT.AUTHORIZATION.CREATED", other))
    assert order_row(api, order["order_id"]).authorization_id == "AUTH-FROM-WEBHOOK"


def test_capture_completed_confirms_capture(api, fake) -> None:
    order, auth_id = held_order(api, fake)
    resource = {
        "id": "CAPTURE-1",
        "status": "COMPLETED",
        "custom_id": order["order_id"],
        "supplementary_data": {"related_ids": {"authorization_id": auth_id}},
    }
    assert send(api, webhook_event("PAYMENT.CAPTURE.COMPLETED", resource)).status_code == 200
    row = order_row(api, order["order_id"])
    assert (row.status, row.capture_id) == ("captured", "CAPTURE-1")


def test_authorization_voided_confirms_void(api, fake) -> None:
    order, auth_id = held_order(api, fake)
    resource = {"id": auth_id, "status": "VOIDED", "custom_id": order["order_id"]}
    assert send(api, webhook_event("PAYMENT.AUTHORIZATION.VOIDED", resource)).status_code == 200
    assert order_row(api, order["order_id"]).status == "voided"
    # The reviewer's void call then returns the stored result without calling PayPal.
    void = api.post(f"/payments/authorizations/{auth_id}/void", json={"actor": "reviewer"})
    assert void.status_code == 200
    assert fake.calls("POST", "/void") == []


def test_events_that_cant_move_the_order_change_nothing(api, fake) -> None:
    order = create(api)
    resource = {"id": "AUTH-X", "custom_id": order["order_id"]}
    send(api, webhook_event("PAYMENT.AUTHORIZATION.VOIDED", resource))  # created, not held
    assert order_row(api, order["order_id"]).status == "created"
    dispute = webhook_event("CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-1"})
    assert send(api, dispute).status_code == 200
    unknown = webhook_event("PAYMENT.SALE.COMPLETED", {"id": "S-1"})
    assert send(api, unknown).status_code == 200
    assert {r.event_type for r in webhook_rows(api)} == {
        "PAYMENT.AUTHORIZATION.VOIDED",
        "CUSTOMER.DISPUTE.CREATED",
        "PAYMENT.SALE.COMPLETED",
    }


def test_oversized_body_is_refused_before_verification(api, fake) -> None:
    big = webhook_event("CHECKOUT.ORDER.APPROVED", {"id": "X", "pad": "x" * (300 * 1024)})
    response = send(api, big)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"

    def chunks():  # no Content-Length: the streamed size is still capped
        yield b'{"id": "WH-1", "event_type": "X", "pad": "'
        for _ in range(300):
            yield b"x" * 1024
        yield b'"}'

    streamed = api.post("/webhooks/paypal", content=chunks(), headers=webhook_headers())
    assert streamed.status_code == 400
    assert fake.calls("POST", "/verify-webhook-signature") == []
    assert webhook_rows(api) == []
