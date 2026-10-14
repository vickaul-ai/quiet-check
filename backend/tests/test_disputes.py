"""D8: dispute webhooks -> stored dispute + evidence draft; the reviewer submits (fake PayPal)."""

import json

from sqlalchemy import select, update

from app.db import disputes, events, receipts
from app.disputes import MAX_NOTE
from app.identifiers import identifier_hash, normalise
from tests.fake_paypal import dispute_webhook, webhook_event
from tests.test_payments import ADDRESS, create, processed_order, rows
from tests.test_webhooks import send

BUYER_EMAIL = "disputing.buyer@example.com"
NOTE = "Dispute evidence draft: receipts and the decision log, as written by the fake model."


def captured_order(api, fake) -> dict:
    """A clean order, authorised and captured by the policy (stored check, no members)."""
    order = processed_order(api, fake, "clean")
    detail = api.get(f"/merchant/orders/{order['order_id']}").json()
    assert detail["status"] == "captured" and detail["capture_id"]
    return detail


def open_dispute(api, fake, model, order: dict, **kw) -> dict:
    model.calls[:] = []
    model.reply = NOTE
    dispute = fake.open_dispute(order["capture_id"], buyer_email=BUYER_EMAIL, **kw)
    assert send(api, dispute_webhook(dispute)).status_code == 200
    return dispute


def dispute_row(api, dispute_id: str):
    (row,) = rows(api, disputes, id=dispute_id)
    return row


def submit(api, dispute_id: str, note: str = "Our evidence note.", actor: str = "reviewer"):
    return api.post(
        f"/merchant/disputes/{dispute_id}/evidence", json={"note": note, "actor": actor}
    )


def test_dispute_webhook_stores_the_dispute_and_drafts_evidence(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = open_dispute(api, fake, model, order)
    row = dispute_row(api, dispute["dispute_id"])
    assert row.order_id == order["order_id"]  # matched by capture ID
    assert (row.reason, row.status) == ("UNAUTHORISED", "WAITING_FOR_SELLER_RESPONSE")
    assert (row.amount_value, row.currency_code) == ("49.00", "USD")
    assert (row.draft_note, row.draft_source) == (NOTE, "llm")
    assert row.drafted_at and row.submitted_at is None
    assert len(model.calls) == 1
    assert fake.calls("GET", f"/disputes/{dispute['dispute_id']}")  # read again from PayPal

    (webhook,) = [
        r for r in rows(api, events, order_id=order["order_id"]) if r.source == "paypal_webhook"
    ]
    assert webhook.event_type == "CUSTOMER.DISPUTE.CREATED"
    assert webhook.resource_id == dispute["dispute_id"]
    drafted = [r for r in rows(api, events, event_type="dispute.drafted")]
    assert [r.payload for r in drafted] == [
        {"dispute_id": dispute["dispute_id"], "reason": "UNAUTHORISED", "draft_source": "llm"}
    ]
    assert drafted[0].order_id == order["order_id"]

    detail = api.get(f"/merchant/orders/{order['order_id']}").json()
    (body,) = detail["disputes"]
    assert body["dispute_id"] == dispute["dispute_id"]
    assert body["draft_note"] == NOTE and body["draft_source"] == "llm"
    assert body["amount"] == {"currency_code": "USD", "value": "49.00"}
    assert body["seller_response_due_date"].endswith("Z")
    assert BUYER_EMAIL not in json.dumps(body)
    assert detail["dispute_status"] == "WAITING_FOR_SELLER_RESPONSE"
    listed = api.get("/merchant/orders").json()["orders"]
    assert listed[0]["order_id"] == order["order_id"]
    assert listed[0]["dispute_status"] == "WAITING_FOR_SELLER_RESPONSE"


def test_updated_webhook_refreshes_status_without_redrafting(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = open_dispute(api, fake, model, order, status="UNDER_REVIEW")
    fake.disputes[dispute["dispute_id"]]["status"] = "RESOLVED"
    update_event = dispute_webhook(dispute, "CUSTOMER.DISPUTE.UPDATED")
    update_event["resource"] = {"dispute_id": dispute["dispute_id"]}  # partial resource
    assert send(api, update_event).status_code == 200
    row = dispute_row(api, dispute["dispute_id"])
    assert row.status == "RESOLVED" and row.draft_source == "llm"
    assert len(model.calls) == 1


def test_match_by_custom_when_the_capture_id_is_unknown(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = open_dispute(api, fake, model, order, seller_transaction_id="NOT-OUR-CAPTURE")
    row = dispute_row(api, dispute["dispute_id"])
    assert row.order_id == order["order_id"] and row.draft_source == "llm"


def test_unmatched_dispute_is_stored_with_no_order_and_no_draft(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = fake.open_dispute(order["capture_id"], seller_transaction_id="SOMEONE-ELSES")
    dispute["disputed_transactions"][0]["custom"] = "ord_NOT_OURS"
    model.calls[:] = []
    assert send(api, dispute_webhook(dispute)).status_code == 200
    row = dispute_row(api, dispute["dispute_id"])
    assert row.order_id is None and row.draft_note is None and row.draft_source is None
    assert model.calls == []
    assert api.get(f"/merchant/orders/{order['order_id']}").json()["disputes"] == []


def test_model_sees_no_payer_identifiers_or_hashes(api, fake, model) -> None:
    order = captured_order(api, fake)
    open_dispute(api, fake, model, order)
    ((system, prompt), (human, sheet)) = model.calls[0]
    assert (system, human) == ("system", "human")
    assert "dispute" in prompt.lower()
    detail = api.get(f"/merchant/orders/{order['order_id']}").json()
    forbidden = [
        "ana@example.com",
        BUYER_EMAIL,
        "Test Buyer",
        detail["device_id"],
        # Long enough not to turn up by chance inside PayPal's random hex IDs.
        *[ADDRESS[k] for k in ("address_line_1", "admin_area_2", "postal_code")],
        normalise("shipping_address", ADDRESS),
        identifier_hash("email", "ana@example.com"),
        identifier_hash("shipping_address", ADDRESS),
        identifier_hash("device_id", detail["device_id"]),
    ]
    for value in forbidden:
        assert value not in sheet, value
    facts = json.loads(sheet)
    assert set(facts) == {
        "dispute",
        "order",
        "paypal_ids",
        "decision",
        "consortium_check",
        "receipts",
        "paypal_webhooks_verified",
    }
    assert facts["paypal_ids"]["capture_id"] == order["capture_id"]
    assert facts["receipts"]["count"] == 9 and facts["receipts"]["verified"] == 9
    assert {r["receipt_id"] for r in facts["receipts"]["items"]} == {
        r["receipt_id"] for r in detail["receipts"]
    }
    assert facts["consortium_check"]["hits"] == []
    assert facts["decision"]["decision"] == "clean"
    assert facts["order"]["captured_by"] == "policy"


def test_model_failure_falls_back_to_the_template(api, fake, model) -> None:
    order = captured_order(api, fake)
    model.mode = "error"
    dispute = open_dispute(api, fake, model, order)
    row = dispute_row(api, dispute["dispute_id"])
    assert row.draft_source == "template"
    for value in (dispute["dispute_id"], order["order_id"], order["capture_id"]):
        assert value in row.draft_note
    detail = api.get(f"/merchant/orders/{order['order_id']}").json()
    assert all(r["receipt_id"] in row.draft_note for r in detail["receipts"])
    assert "not proof of the buyer's identity" in row.draft_note
    assert len(row.draft_note) <= MAX_NOTE
    assert "ana@example.com" not in row.draft_note and BUYER_EMAIL not in row.draft_note
    (drafted,) = rows(api, events, event_type="dispute.drafted")
    assert drafted.payload["draft_source"] == "template"


def test_cut_off_reply_falls_back_to_the_template(api, fake, model) -> None:
    order = captured_order(api, fake)
    model.mode = "cut_off"
    dispute = open_dispute(api, fake, model, order)
    row = dispute_row(api, dispute["dispute_id"])
    assert row.draft_source == "template"
    assert "not proof of the buyer's identity" in row.draft_note


def test_template_and_long_replies_fit_paypal_limit(api, fake, model) -> None:
    order = captured_order(api, fake)
    # Real AP3 session IDs are long; the template drops them before going over 2000 characters.
    with api.app.state.engine.begin() as conn:
        conn.execute(update(receipts).values(session_id="f" * 120))
    model.mode = "error"
    dispute = open_dispute(api, fake, model, order)
    note = dispute_row(api, dispute["dispute_id"]).draft_note
    assert len(note) <= MAX_NOTE and "f" * 120 not in note
    with api.app.state.engine.connect() as conn:
        assert all(r.id in note for r in conn.execute(select(receipts)).all())

    model.mode = "ok"
    long_reply = "**Dispute** " + "This sentence is factual. " * 120
    other = captured_order(api, fake)
    model.reply = long_reply
    second = fake.open_dispute(other["capture_id"])
    send(api, dispute_webhook(second))
    note = dispute_row(api, second["dispute_id"]).draft_note
    assert len(note) <= MAX_NOTE and note.endswith(".") and "**" not in note


def test_submit_evidence_happy_path_and_idempotent(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = open_dispute(api, fake, model, order)
    dispute_id = dispute["dispute_id"]
    response = submit(api, dispute_id, note="  Edited by the reviewer. Receipts rcp_1.  ")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submitted_note"] == "Edited by the reviewer. Receipts rcp_1."
    assert body["submitted_by"] == "reviewer" and body["submitted_at"].endswith("Z")
    assert body["status"] == "UNDER_REVIEW"  # PayPal moved it on; read again after submitting
    (call,) = fake.calls("POST", "/provide-evidence")
    assert call.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert call.headers["paypal-request-id"] == f"{dispute_id}:evidence"
    assert fake.evidence[dispute_id] == [
        {"evidence_type": "OTHER", "notes": "Edited by the reviewer. Receipts rcp_1."}
    ]
    (submitted,) = rows(api, events, event_type="dispute.evidence_submitted")
    assert submitted.order_id == order["order_id"]
    assert submitted.payload == {
        "dispute_id": dispute_id,
        "actor": "reviewer",
        "dispute_status": "WAITING_FOR_SELLER_RESPONSE",
    }

    again = submit(api, dispute_id, note="A different note")
    assert again.status_code == 200 and again.json()["submitted_note"] == body["submitted_note"]
    assert len(fake.calls("POST", "/provide-evidence")) == 1
    assert len(rows(api, events, event_type="dispute.evidence_submitted")) == 1


def test_submit_refused_when_paypal_isnt_asking_for_evidence(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = open_dispute(api, fake, model, order, status="UNDER_REVIEW")
    response = submit(api, dispute["dispute_id"])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "dispute_not_awaiting_evidence"
    assert error["details"] == {"dispute_status": "UNDER_REVIEW"}
    assert fake.calls("POST", "/provide-evidence") == []
    assert dispute_row(api, dispute["dispute_id"]).submitted_at is None

    # The sandbox's require-evidence moves it to the seller; then the same click works.
    fake.disputes[dispute["dispute_id"]]["status"] = "WAITING_FOR_SELLER_RESPONSE"
    assert submit(api, dispute["dispute_id"]).status_code == 200


def test_submit_sends_the_evidence_type_paypal_asked_for(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = open_dispute(api, fake, model, order, requested_evidence="PROOF_OF_RECEIPT_COPY")
    assert submit(api, dispute["dispute_id"], note="Our records.").status_code == 200
    assert fake.evidence[dispute["dispute_id"]] == [
        {"evidence_type": "PROOF_OF_RECEIPT_COPY", "notes": "Our records."}
    ]


def test_submit_refused_when_paypal_wants_tracking(api, fake, model) -> None:
    # Live 2026-10-14: an item-not-received inquiry asked for PROOF_OF_FULFILLMENT, and PayPal
    # refused a note with EVIDENCE_TYPE_IS_NOT_ALLOWED. We say so instead of calling PayPal.
    order = captured_order(api, fake)
    dispute = open_dispute(
        api,
        fake,
        model,
        order,
        reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        requested_evidence="PROOF_OF_FULFILLMENT",
    )
    response = submit(api, dispute["dispute_id"])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "evidence_needs_details"
    assert error["details"]["requested_evidence_type"] == "PROOF_OF_FULFILLMENT"
    assert fake.calls("POST", "/provide-evidence") == []
    assert dispute_row(api, dispute["dispute_id"]).submitted_at is None


def test_submit_validation_and_unknown_dispute(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute_id = open_dispute(api, fake, model, order)["dispute_id"]
    for note, actor in (("x" * (MAX_NOTE + 1), "reviewer"), ("   ", "reviewer"), ("ok", "policy")):
        response = submit(api, dispute_id, note=note, actor=actor)
        assert response.status_code == 422, (len(note), actor)
        assert response.json()["error"]["code"] == "validation_error"
    extra = api.post(
        f"/merchant/disputes/{dispute_id}/evidence",
        json={"note": "ok", "actor": "reviewer", "evidence_type": "OTHER"},
    )
    assert extra.status_code == 422
    assert submit(api, dispute_id, note="x" * MAX_NOTE).status_code == 200
    assert submit(api, "PP-D-MISSING").status_code == 404
    assert fake.calls("POST", "/provide-evidence") and len(fake.evidence[dispute_id]) == 1


def test_unverified_dispute_webhook_is_ignored(api, fake, model) -> None:
    order = captured_order(api, fake)
    dispute = fake.open_dispute(order["capture_id"])
    model.calls[:] = []
    response = send(api, dispute_webhook(dispute), signature="forged")
    assert response.status_code == 400
    assert rows(api, disputes) == []
    assert fake.calls("GET", f"/disputes/{dispute['dispute_id']}") == []
    assert model.calls == []


def test_dispute_on_an_order_without_a_check_still_drafts(api, fake, model) -> None:
    """The fact sheet copes with missing pieces (no check, no decision, no capture)."""
    order = create(api)
    model.mode = "error"
    resource = {
        "dispute_id": "PP-D-NOCHECK",
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "OPEN",
        "disputed_transactions": [{"custom": order["order_id"]}],
    }
    assert send(api, webhook_event("CUSTOMER.DISPUTE.CREATED", resource)).status_code == 200
    row = dispute_row(api, "PP-D-NOCHECK")  # PayPal doesn't know it: stored from the webhook
    assert row.order_id == order["order_id"] and row.draft_source == "template"
    assert "shipping and tracking evidence is not part" in row.draft_note
