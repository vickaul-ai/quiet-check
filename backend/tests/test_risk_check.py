"""POST /risk/check against the three real member processes (D2 acceptance)."""

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from app.config import MEMBERS
from app.consortium.initiator import verify_receipt
from app.db import checks, events, orders, receipts
from app.identifiers import FIELDS
from app.ids import new_id
from app.main import create_app
from tests import cases
from tests.conftest import make_settings
from tests.harness import MemberProcess, free_port


def make_order(app, identifiers: dict | None = None) -> str:
    """An order as D3 will leave it after approval: payer email and shipping address from
    PayPal, device ID from order creation. Without identifiers, the order isn't approved yet."""
    identifiers = identifiers or {}
    order_id = new_id("ord")
    with app.state.engine.begin() as conn:
        conn.execute(
            insert(orders).values(
                id=order_id,
                paypal_order_id=f"PP-{order_id}",
                status="checking",
                items=[
                    {
                        "sku": "backpack-blue-m",
                        "name": "Backpack",
                        "quantity": 1,
                        "unit_amount": {"currency_code": "USD", "value": "49.00"},
                    }
                ],
                amount_value="49.00",
                currency_code="USD",
                device_id=identifiers.get("device_id", "dev-x"),
                payer_email=identifiers.get("email"),
                shipping_address=identifiers.get("shipping_address"),
            )
        )
    return order_id


def run_check(client: TestClient, identifiers: dict | None = None, order_id: str | None = None):
    """Store the identifiers on a new order (unless order_id is given), then check the order."""
    order_id = order_id or make_order(client.app, identifiers)
    return client.post("/risk/check", json={"order_id": order_id})


@pytest.mark.parametrize(
    ("name", "identifiers", "expected"), cases.CASES, ids=[c[0] for c in cases.CASES]
)
def test_seeded_cases(client, name, identifiers, expected) -> None:
    response = run_check(client, identifiers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(h["member"], h["field"]) for h in body["hits"]] == [
        (m, f) for m in MEMBERS for f in FIELDS
    ]
    assert {(h["member"], h["field"]) for h in body["hits"] if h["is_match"]} == expected
    assert body["hit_count"] == len(expected)
    assert len(body["receipts"]) == 9
    assert all(r["verified"] for r in body["receipts"])
    assert body["check_id"].startswith("chk_")
    assert all(r["receipt_id"].startswith("rcp_") for r in body["receipts"])


def test_stored_receipts_reverify_and_tampering_fails(client) -> None:
    body = run_check(client, cases.person("ben")).json()
    initiator_key = client.app.state.initiator.public_key
    with client.app.state.engine.connect() as conn:
        rows = conn.execute(select(receipts).where(receipts.c.check_id == body["check_id"])).all()
    assert len(rows) == 9
    for row in rows:
        assert [d["ap3_session_id"] for d in row.intent_directives] == [row.session_id] * 2
        assert row.result_directive["attestation"] == "experimental_placeholders"
        assert row.member_commitment["agent_id"] == f"member_{row.member.lower()}"
        assert verify_receipt(
            row.intent_directives,
            row.result_directive,
            row.member_commitment,
            bytes.fromhex(row.member_public_key),
            initiator_key,
        )
    row = rows[0]
    forged = copy.deepcopy(row.result_directive)
    forged["result_data"]["metadata"]["description"] = '{"is_match": true}'
    assert not verify_receipt(
        row.intent_directives,
        forged,
        row.member_commitment,
        bytes.fromhex(row.member_public_key),
        initiator_key,
    )
    other_member = next(r for r in rows if r.member != row.member)
    assert not verify_receipt(
        row.intent_directives,
        row.result_directive,
        row.member_commitment,
        bytes.fromhex(other_member.member_public_key),
        initiator_key,
    )


def test_repeat_call_returns_stored_check(client) -> None:
    order_id = make_order(client.app, cases.person("cy"))
    first = run_check(client, order_id=order_id).json()
    # Even if the order's identifiers changed, the stored check comes back; PSI doesn't rerun.
    with client.app.state.engine.begin() as conn:
        conn.execute(update(orders).where(orders.c.id == order_id).values(payer_email="x@y.z"))
    second = run_check(client, order_id=order_id)
    assert second.status_code == 200
    assert second.json() == first
    with client.app.state.engine.connect() as conn:
        stored = conn.execute(
            select(receipts).where(receipts.c.check_id == first["check_id"])
        ).all()
        kinds = (
            conn.execute(select(events.c.event_type).where(events.c.order_id == order_id))
            .scalars()
            .all()
        )
    assert len(stored) == 9
    assert kinds == ["check.completed"]


def test_running_check_is_409(client) -> None:
    order_id = make_order(client.app, cases.person("ana"))
    with client.app.state.engine.begin() as conn:
        conn.execute(insert(checks).values(id=new_id("chk"), order_id=order_id, status="running"))
    response = run_check(client, order_id=order_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_unknown_order_is_404(client) -> None:
    response = run_check(client, order_id="ord_doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_order_without_payer_details_is_409_and_creates_no_check(client) -> None:
    order_id = make_order(client.app)  # not approved: no payer email or address yet
    response = run_check(client, order_id=order_id)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "invalid_state"
    assert error["details"]["missing"] == ["email", "shipping_address"]
    with client.app.state.engine.connect() as conn:
        assert conn.execute(select(checks).where(checks.c.order_id == order_id)).first() is None


def test_caller_supplied_identifiers_are_rejected(client) -> None:
    """The check must use the order's own identifiers, so the request can't carry any."""
    order_id = make_order(client.app, cases.person("ben"))
    response = client.post(
        "/risk/check",
        json={"order_id": order_id, "identifiers": cases.person("ana")},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["fields"] == ["identifiers"]
    assert "ana@example.com" not in response.text


def test_initiator_not_on_allowlist_is_refused(run_dir, consortium) -> None:
    """Members answer only allowlisted initiators: an app on any other port gets 502."""
    urls = {m: p.url for m, p in consortium["members"].items()}
    app = create_app(make_settings(run_dir, "not-allowed", urls, free_port()))
    with TestClient(app) as client:
        response = run_check(client, cases.person("ben"))
    assert response.status_code == 502
    assert response.json()["error"]["details"]["members"] == ["A", "B", "C"]


def test_member_down_fails_whole_check_then_rerun_succeeds(run_dir, consortium) -> None:
    """All-or-nothing: one unreachable member fails the check (502, no receipts, no hits).
    A repeat call after the member is back reruns PSI on the same check row."""
    dead_port = free_port()
    urls = {m: p.url for m, p in consortium["members"].items()}
    urls["C"] = f"http://127.0.0.1:{dead_port}"
    port = consortium["initiator_ports"]["member-down"]
    app = create_app(make_settings(run_dir, "member-down", urls, port, check_timeout_s=20))
    with TestClient(app) as client:
        order_id = make_order(app, cases.person("cy"))
        failed = run_check(client, order_id=order_id)
        assert failed.status_code == 502
        error = failed.json()["error"]
        assert error["code"] == "member_unavailable"
        assert error["details"]["members"] == ["C"]
        check_id = error["details"]["check_id"]
        with app.state.engine.connect() as conn:
            check = conn.execute(select(checks).where(checks.c.id == check_id)).first()
            assert (check.status, check.error, check.hit_count) == (
                "failed",
                "member_unavailable",
                None,
            )
            assert conn.execute(select(receipts).where(receipts.c.check_id == check_id)).all() == []

        member_c = MemberProcess(
            "C", dead_port, consortium["data_dir"], run_dir / "logs", consortium["initiator_urls"]
        ).wait_ready()
        try:
            rerun = run_check(client, order_id=order_id)
        finally:
            member_c.stop()
        assert rerun.status_code == 200, rerun.text
        assert rerun.json()["check_id"] == check_id
        assert rerun.json()["hit_count"] == 4
        with app.state.engine.connect() as conn:
            kinds = (
                conn.execute(select(events.c.event_type).where(events.c.order_id == order_id))
                .scalars()
                .all()
            )
        assert kinds == ["check.failed", "check.completed"]


def test_member_key_is_stored_and_used(client, consortium) -> None:
    """The member's key is written once (0600) and is the key its card and receipts carry,
    so a restart (which reloads key.json) keeps the member's identity."""
    import json

    body = run_check(client, cases.person("ana")).json()
    with client.app.state.engine.connect() as conn:
        rows = conn.execute(select(receipts).where(receipts.c.check_id == body["check_id"])).all()
    for member in MEMBERS:
        key_file = consortium["data_dir"] / member / "key.json"
        assert oct(key_file.stat().st_mode & 0o777) == "0o600"
        public_key = json.loads(key_file.read_text())["public_key"]
        assert {r.member_public_key for r in rows if r.member == member} == {public_key}
