"""D9 follow-up: /health/deep and the stuck-order sweep."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import MEMBERS
from app.consortium.common import list_digest, load_list
from app.db import events, orders
from app.main import create_app
from app.sweep import MAX_SWEEPS, sweep_once
from tests.conftest import make_settings
from tests.harness import free_port
from tests.stored_checks import store_check
from tests.test_payments import ADDRESS, create, rows

# /health/deep


def _digests(data_dir) -> dict[str, str]:
    return {m: list_digest(load_list(data_dir / m / "list.csv")) for m in MEMBERS}


def test_deep_health_reports_members_and_lists(run_dir, consortium) -> None:
    urls = {m: p.url for m, p in consortium["members"].items()}
    expected = _digests(consortium["data_dir"])
    expected["B"] = "0" * 64  # as if Member B had been seeded with a wrong email
    settings = make_settings(
        run_dir, "deep-health", urls, free_port(), expected_member_hashes=expected
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/health/deep").json()
    assert body["database"] == {"ok": True}
    assert body["initiator"]["url"].startswith("http://127.0.0.1:")
    assert body["workflow"] == {"configured": False, "task": None}
    by_member = {m["member"]: m for m in body["members"]}
    for m in ("A", "C"):
        assert by_member[m]["ok"] and by_member[m]["url_matches"]
        assert by_member[m]["list_matches_expected"] is True
        assert by_member[m]["declared_url"] == urls[m]
    assert by_member["B"]["list_matches_expected"] is False and not by_member["B"]["ok"]
    assert body["ok"] is False
    # Nothing from the lists or identifiers in the response.
    text = str(body)
    assert all(h not in text for h in expected.values() if h != "0" * 64)


def test_deep_health_without_expected_hashes_and_member_down(run_dir, consortium) -> None:
    urls = {m: p.url for m, p in consortium["members"].items()}
    urls["C"] = f"http://127.0.0.1:{free_port()}"  # nothing listening
    settings = make_settings(run_dir, "deep-health-down", urls, free_port())
    with TestClient(create_app(settings)) as client:
        body = client.get("/health/deep").json()
    by_member = {m["member"]: m for m in body["members"]}
    assert by_member["A"]["ok"] and by_member["A"]["list_matches_expected"] is None
    assert by_member["C"]["ok"] is False and by_member["C"]["error"]
    assert body["ok"] is False


def test_deep_health_needs_the_token(run_dir) -> None:
    settings = make_settings(
        run_dir, "deep-health-token", {}, free_port(), start_initiator=False, api_token="t"
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/deep").status_code == 401
        assert client.get("/health").status_code == 200


# Stuck-order sweep


def _make_stuck(api, order_id: str, minutes: int = 5) -> None:
    with api.app.state.engine.begin() as conn:
        conn.execute(
            update(orders)
            .where(orders.c.id == order_id)
            .values(status="checking", updated_at=datetime.now(UTC) - timedelta(minutes=minutes))
        )


def test_sweep_reruns_a_stuck_order_and_caps_it(api, fake) -> None:
    order = create(api)
    oid = order["order_id"]
    fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
    store_check(api.app.state.engine, oid, "hold")
    _make_stuck(api, oid)
    fresh = create(api)  # checking, but only just: a live run, not stuck
    with api.app.state.engine.begin() as conn:
        conn.execute(
            update(orders).where(orders.c.id == fresh["order_id"]).values(status="checking")
        )

    assert asyncio.run(sweep_once(api.app.state)) == [oid]
    assert api.get(f"/merchant/orders/{oid}/status").json()["status"] == "held"
    started = [
        r.payload for r in rows(api, events, order_id=oid) if r.event_type == "risk_agent.started"
    ]
    assert started == [{"runner": "in_process", "reason": "sweep"}]
    assert api.get(f"/merchant/orders/{fresh['order_id']}/status").json()["status"] == "checking"

    # Cap: an order that keeps getting stuck is swept at most MAX_SWEEPS times.
    for _ in range(MAX_SWEEPS + 2):
        _make_stuck(api, oid)
        asyncio.run(sweep_once(api.app.state))
    sweeps = [r for r in rows(api, events, order_id=oid) if r.payload.get("reason") == "sweep"]
    assert len(sweeps) == MAX_SWEEPS
