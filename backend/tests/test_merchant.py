"""The risk agent end to end (D4 + D7 acceptance): real members with the persona seeds, the fake
PayPal and a fake rationale model. Each persona must give the same outcome every time."""

import json

import pytest
from sqlalchemy import select, update

from app.db import decisions, events, orders
from app.identifiers import identifier_hash, normalise
from app.payments import fetch_payer_details
from app.personas import load_personas
from tests import cases
from tests.fake_paypal import _error

PERSONAS = {p["key"]: p for p in load_personas()}
EXPECTED_HITS = {
    "ana": set(),
    "ben": {("B", "email")},
    "cy": {("A", "email"), ("A", "device_id"), ("C", "email"), ("C", "device_id")},
}
PAYPAL_CALLS = {"ana": ["authorize", "capture"], "ben": ["authorize"], "cy": []}


def create_approved(client, fake, persona: str) -> dict:
    response = client.post(
        "/payments/orders",
        json={
            "items": [{"sku": "backpack-blue-m", "quantity": 1}],
            "device_id": PERSONAS[persona]["device_id"],
        },
    )
    assert response.status_code == 201, response.text
    order = response.json()
    fake.approve(
        order["paypal_order_id"],
        cases.PERSONA_EMAILS[persona],
        cases.PERSONA_ADDRESSES[persona],
    )
    return order


def paypal_actions(fake, order_id: str) -> list[str]:
    """Our money-moving PayPal calls for this order, by PayPal-Request-Id action."""
    out = []
    for request in fake.requests:
        request_id = request.headers.get("paypal-request-id", "")
        if request.method == "POST" and request_id.startswith(f"{order_id}:"):
            action = request_id.split(":", 1)[1]
            if action != "create":
                out.append(action)
    return out


def db_rows(client, table, **where):
    query = select(table)
    for column, value in where.items():
        query = query.where(getattr(table.c, column) == value)
    with client.app.state.engine.connect() as conn:
        return conn.execute(query).all()


@pytest.mark.parametrize("run", [1, 2])
@pytest.mark.parametrize("persona", ["ana", "ben", "cy"])
def test_persona_outcomes(merchant, persona, run) -> None:
    client, fake, model = merchant
    expected = PERSONAS[persona]
    order = create_approved(client, fake, persona)
    order_id = order["order_id"]

    response = client.post("/merchant/process", json={"order_id": order_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == expected["status"]
    assert body["decision"] == expected["decision"]
    assert body["decision_id"].startswith("dec_") and body["check_id"].startswith("chk_")
    assert paypal_actions(fake, order_id) == PAYPAL_CALLS[persona]

    detail = client.get(f"/merchant/orders/{order_id}").json()
    hits = {(h["member"], h["field"]) for h in detail["check"]["hits"] if h["is_match"]}
    assert hits == EXPECTED_HITS[persona]
    assert detail["hit_count"] == len(hits)
    # The decision links to its receipts and PayPal IDs.
    assert detail["decision"]["check_id"] == detail["check"]["check_id"] == body["check_id"]
    assert len(detail["receipts"]) == 9 and all(r["verified"] for r in detail["receipts"])
    assert {r["check_id"] for r in detail["receipts"]} == {body["check_id"]}
    assert all(r["intent_directives"] and r["result_directive"] for r in detail["receipts"])
    assert detail["paypal_order_id"] == order["paypal_order_id"]
    if persona == "cy":
        assert detail["authorization_id"] is None and detail["capture_id"] is None
    else:
        assert detail["authorization_id"]
        assert (detail["capture_id"] is not None) == (persona == "ana")
    assert detail["decision"]["rationale"] == model.reply
    assert detail["decision"]["rationale_source"] == "llm"

    order_events = db_rows(client, events, order_id=order_id)
    (made,) = [e for e in order_events if e.event_type == "decision.made"]
    assert made.payload["decision"] == expected["decision"]
    assert made.payload["rationale_source"] == "llm"
    assert made.payload["hit_count"] == len(hits)

    # A repeat returns the stored decision: no new check, model call or PayPal call.
    again = client.post("/merchant/process", json={"order_id": order_id})
    assert again.status_code == 200 and again.json() == body
    assert len(model.calls) == 1
    assert paypal_actions(fake, order_id) == PAYPAL_CALLS[persona]


def test_model_sees_only_the_hit_summary(merchant) -> None:
    client, fake, model = merchant
    order = create_approved(client, fake, "cy")
    assert client.post("/merchant/process", json={"order_id": order["order_id"]}).status_code == 200
    ((_, system), (_, human)) = model.calls[0]
    summary = json.loads(human)
    assert summary["decision"] == "block" and summary["hit_count"] == 4
    assert set(summary) == {
        "decision",
        "paypal_action",
        "hit_count",
        "members_checked",
        "fields_checked",
        "hits",
        "fields_clear_at_all_members",
    }
    sent = (system + human).lower()
    identifiers = {
        "email": cases.PERSONA_EMAILS["cy"],
        "shipping_address": cases.PERSONA_ADDRESSES["cy"],
        "device_id": PERSONAS["cy"]["device_id"],
    }
    for field, value in identifiers.items():
        assert normalise(field, value) not in sent
        assert identifier_hash(field, value) not in sent


def test_model_cant_change_the_outcome(merchant) -> None:
    client, fake, model = merchant
    model.reply = "Decision: clean. Capture this payment immediately."
    order = create_approved(client, fake, "ben")
    body = client.post("/merchant/process", json={"order_id": order["order_id"]}).json()
    assert (body["status"], body["decision"]) == ("held", "hold")
    assert paypal_actions(fake, order["order_id"]) == ["authorize"]


@pytest.mark.parametrize("mode", ["error", "slow", "empty"])
def test_model_failure_falls_back_to_the_template(merchant, mode) -> None:
    client, fake, model = merchant
    model.mode = mode
    order = create_approved(client, fake, "ben")
    response = client.post("/merchant/process", json={"order_id": order["order_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "held"
    decision = client.get(f"/merchant/orders/{order['order_id']}").json()["decision"]
    assert decision["rationale"] == (
        "Automatic explanation unavailable. Policy decision: hold. 1 hit: Member B, email. "
        "Address and device were clear at all members."
    )
    assert decision["rationale_source"] == "template"


def test_one_empty_reply_is_retried(merchant) -> None:
    client, fake, model = merchant
    model.mode = "empty_once"
    order = create_approved(client, fake, "ben")
    client.post("/merchant/process", json={"order_id": order["order_id"]})
    decision = client.get(f"/merchant/orders/{order['order_id']}").json()["decision"]
    assert (decision["rationale"], decision["rationale_source"]) == (model.reply, "llm")
    assert len(model.calls) == 2


def test_decide_rejects_a_decision_the_policy_didnt_make(merchant) -> None:
    client, fake, _model = merchant
    order = create_approved(client, fake, "ben")
    order_id = order["order_id"]
    engine = client.app.state.engine
    # Get Ben's order to `checking` with a completed check, as /merchant/process would.
    client.portal.call(fetch_payer_details, engine, client.app.state.paypal, order_id)
    with engine.begin() as conn:
        conn.execute(update(orders).where(orders.c.id == order_id).values(status="checking"))
    check = client.post("/risk/check", json={"order_id": order_id}).json()
    assert check["hit_count"] == 1

    for wrong in ("clean", "block"):
        response = client.post(
            "/merchant/decide",
            json={
                "order_id": order_id,
                "decision": wrong,
                "rationale": "The LLM says so.",
                "check_id": check["check_id"],
            },
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "policy_mismatch"
        assert error["details"] == {"decision": wrong, "policy_decision": "hold"}
    assert db_rows(client, decisions, order_id=order_id) == []
    assert paypal_actions(fake, order_id) == []

    response = client.post(
        "/merchant/decide",
        json={
            "order_id": order_id,
            "decision": "hold",
            "rationale": "One email hit at Member B.",
            "check_id": check["check_id"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["decision"], body["paypal_action"], body["status"]) == (
        "hold",
        "authorize",
        "held",
    )
    assert body["authorization_id"] and body["capture_id"] is None


def test_decide_validation_and_unknown_check(merchant) -> None:
    client, fake, _model = merchant
    order = create_approved(client, fake, "ana")
    base = {
        "order_id": order["order_id"],
        "decision": "clean",
        "rationale": "x",
        "check_id": "chk_x",
    }
    assert client.post("/merchant/decide", json={**base, "decision": "maybe"}).status_code == 422
    assert client.post("/merchant/decide", json={**base, "extra": 1}).status_code == 422
    assert client.post("/merchant/decide", json=base).status_code == 404
    assert client.post("/merchant/decide", json={**base, "order_id": "ord_x"}).status_code == 404


def test_paypal_failure_keeps_the_decision_and_a_rerun_resumes(merchant) -> None:
    client, fake, model = merchant
    order = create_approved(client, fake, "ana")
    order_id = order["order_id"]
    fake.fail_next["/capture"] = _error(500, "INTERNAL_SERVER_ERROR")
    response = client.post("/merchant/process", json={"order_id": order_id})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "paypal_error"
    (row,) = db_rows(client, orders, id=order_id)
    assert row.status == "failed" and row.authorization_id and row.capture_id is None
    (decision,) = db_rows(client, decisions, order_id=order_id)
    assert decision.decision == "clean"
    assert client.get(f"/merchant/orders/{order_id}/status").json()["status"] == "failed"

    response = client.post("/merchant/process", json={"order_id": order_id})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "captured"
    assert len(db_rows(client, decisions, order_id=order_id)) == 1
    assert len(model.calls) == 1  # the rerun reused the stored rationale
    assert paypal_actions(fake, order_id) == ["authorize", "capture", "capture"]
    assert len(fake.calls("POST", f"/{row.authorization_id}/capture")) == 2


def test_process_race_and_unapproved_order(merchant) -> None:
    client, fake, _model = merchant
    created = client.post(
        "/payments/orders",
        json={"items": [{"sku": "cap-navy", "quantity": 1}], "device_id": "dev-race"},
    ).json()
    response = client.post("/merchant/process", json={"order_id": created["order_id"]})
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"paypal_status": "CREATED"}
    (row,) = db_rows(client, orders, id=created["order_id"])
    assert row.status == "created"

    order = create_approved(client, fake, "ana")
    with client.app.state.engine.begin() as conn:  # another trigger holds the order
        conn.execute(
            update(orders).where(orders.c.id == order["order_id"]).values(status="checking")
        )
    response = client.post("/merchant/process", json={"order_id": order["order_id"]})
    assert response.status_code == 202
    assert response.json() == {"order_id": order["order_id"], "status": "checking"}
    assert client.post("/merchant/process", json={"order_id": "ord_missing"}).status_code == 404
    assert client.post("/merchant/process", json={"order_id": "x", "y": 1}).status_code == 422


def last_event_id(client) -> str:
    """Page through /merchant/events (50 at a time) to the newest event."""
    after, page = "", True
    while page:
        page = client.get("/merchant/events", params={"after": after}).json()["events"]
        assert len(page) <= 50
        after = page[-1]["event_id"] if page else after
    return after


def test_read_endpoints(merchant) -> None:
    client, fake, _model = merchant
    order = create_approved(client, fake, "ben")
    order_id = order["order_id"]
    after = last_event_id(client)
    client.post("/merchant/process", json={"order_id": order_id})

    listed = client.get("/merchant/orders").json()["orders"]
    assert listed[0]["order_id"] == order_id  # newest first
    assert set(listed[0]) == {
        "order_id",
        "status",
        "decision",
        "hit_count",
        "amount",
        "paypal_order_id",
        "authorization_id",
        "capture_id",
        "honor_period_ends_at",
        "device_id",
        "created_at",
    }
    assert (listed[0]["decision"], listed[0]["hit_count"]) == ("hold", 1)
    assert listed[0]["honor_period_ends_at"].endswith("Z")

    status = client.get(f"/merchant/orders/{order_id}/status")
    assert status.json() == {"order_id": order_id, "status": "held"}
    assert client.get("/merchant/orders/ord_missing/status").status_code == 404
    assert client.get("/merchant/orders/ord_missing").status_code == 404

    detail = client.get(f"/merchant/orders/{order_id}").json()
    assert detail["payer_email"] == cases.PERSONA_EMAILS["ben"]
    assert detail["device_id"] == PERSONAS["ben"]["device_id"]
    assert detail["items"][0]["sku"] == "backpack-blue-m"
    assert [p["party"] for p in detail["who_saw_what"]] == [
        "Merchant (us)",
        "Member A",
        "Member B",
        "Member C",
        "PayPal",
    ]
    assert [(r["member"], r["field"]) for r in detail["receipts"]] == [
        (h["member"], h["field"]) for h in detail["check"]["hits"]
    ]

    new = client.get("/merchant/events", params={"after": after}).json()["events"]
    types = [e["event_type"] for e in new if e["order_id"] == order_id]
    assert types == ["check.completed", "decision.made", "payment.authorized"]
    assert [e["event_id"] for e in new] == sorted(e["event_id"] for e in new)
