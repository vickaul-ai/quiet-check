"""D9 hosting: the API token gate, /merchant/start and the Render Workflows trigger, AP3 keys
from env vars, and the hosted URLs derived from Render's env vars."""

import base64
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import Settings
from app.consortium.common import keypair_from_seed, load_keypair
from app.db import events, orders
from app.main import create_app, is_public
from tests.conftest import make_settings, paypal_settings
from tests.harness import free_port
from tests.stored_checks import store_check
from tests.test_payments import ADDRESS, create, event_types, rows

TOKEN = "test-token-123"


@pytest.fixture
def hosted(run_dir, fake, model, request):
    """`api` plus a Render API stand-in; build the app with `make(**settings)`."""
    calls: list[dict] = []
    runs: dict[str, str] = {}
    state = {"status": 202}

    def render_api(req: httpx.Request) -> httpx.Response:
        calls.append(
            {"url": str(req.url), "auth": req.headers["authorization"], **json.loads(req.content)}
        )
        if state["status"] != 202:
            return httpx.Response(state["status"], json={"message": "nope"})
        # Like Render: a repeated idempotency key returns the first run.
        run_id = runs.setdefault(calls[-1]["idempotencyKey"], f"trn-{len(runs) + 1}")
        return httpx.Response(202, json={"id": run_id, "status": "pending"})

    def make(**kw) -> TestClient:
        settings = make_settings(
            run_dir,
            f"hosted-{request.node.name}",
            {},
            free_port(),
            start_initiator=False,
            render_transport=httpx.MockTransport(render_api),
            **paypal_settings(fake, model),
            **kw,
        )
        return TestClient(create_app(settings))

    return make, calls, state


# The API token gate


def test_public_routes() -> None:
    assert is_public("GET", "/health")
    assert is_public("POST", "/webhooks/paypal")
    assert is_public("GET", "/merchant/orders/ord_1/status")
    assert not is_public("GET", "/merchant/orders/ord_1")
    assert not is_public("GET", "/merchant/orders")
    assert not is_public("POST", "/merchant/orders/ord_1/status")
    assert not is_public("GET", "/merchant/orders/ord_1/status/x")
    assert not is_public("POST", "/payments/orders")


def test_token_gate(hosted) -> None:
    make, _calls, _state = hosted
    with make(api_token=TOKEN) as client:
        good = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/health").status_code == 200
        assert client.get("/catalog").status_code == 401
        assert client.get("/catalog", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/catalog", headers=good).status_code == 200
        denied = client.post("/payments/orders", json={"items": [], "device_id": "x"})
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "unauthorized"
        assert client.get("/merchant/orders/ord_missing/status").status_code == 404  # open
        # The webhook stays open (it is verified by signature): no headers -> 400, not 401.
        assert client.post("/webhooks/paypal", json={"id": "x"}).status_code == 400


def test_render_requires_a_token(monkeypatch) -> None:
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="BACKEND_API_TOKEN"):
        create_app(Settings(start_initiator=False))


# /merchant/start and the workflow trigger


def test_start_runs_in_process_without_render(hosted, fake) -> None:
    make, calls, _state = hosted
    with make() as api:
        order = create(api)
        fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
        store_check(api.app.state.engine, order["order_id"], "clean")
        response = api.post("/merchant/start", json={"order_id": order["order_id"]})
        assert response.status_code == 202
        assert response.json() == {"order_id": order["order_id"], "status": "created"}
        # TestClient runs background tasks before returning: the order is decided.
        status = api.get(f"/merchant/orders/{order['order_id']}/status").json()["status"]
        assert status == "captured"
        (started,) = [
            r
            for r in rows(api, events, order_id=order["order_id"])
            if r.event_type == "risk_agent.started"
        ]
        assert started.payload == {"runner": "in_process"}
        # Already decided: 200 with the status, nothing started.
        again = api.post("/merchant/start", json={"order_id": order["order_id"]})
        assert again.status_code == 200 and again.json()["status"] == "captured"
        assert event_types(api, order["order_id"]).count("risk_agent.started") == 1
        assert api.post("/merchant/start", json={"order_id": "ord_missing"}).status_code == 404
    assert calls == []


def test_start_uses_render_workflows_when_configured(hosted, fake) -> None:
    make, calls, _state = hosted
    with make(render_api_key="rnd_test", risk_workflow_task="quietcheck-risk/process_order") as api:
        order = create(api)
        # Before the buyer approves: nothing starts, so the idempotency key isn't used up.
        early = api.post("/merchant/start", json={"order_id": order["order_id"]})
        assert early.status_code == 202 and calls == []
        assert "risk_agent.started" not in event_types(api, order["order_id"])
        fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
        response = api.post("/merchant/start", json={"order_id": order["order_id"]})
        assert response.status_code == 202
        assert calls == [
            {
                "url": "https://api.render.com/v1/task-runs",
                "auth": "Bearer rnd_test",
                "task": "quietcheck-risk/process_order",
                "input": [order["order_id"]],
                "idempotencyKey": order["order_id"],
            }
        ]
        # The task run does the work, not this process.
        assert api.get(f"/merchant/orders/{order['order_id']}/status").json()["status"] == "created"
        (started,) = [
            r
            for r in rows(api, events, order_id=order["order_id"])
            if r.event_type == "risk_agent.started"
        ]
        assert started.payload == {"runner": "render_workflow", "task_run_id": "trn-1"}


def test_start_falls_back_to_in_process_if_render_refuses(hosted, fake) -> None:
    make, calls, state = hosted
    state["status"] = 401
    with make(render_api_key="rnd_bad", risk_workflow_task="quietcheck-risk/process_order") as api:
        order = create(api)
        fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
        store_check(api.app.state.engine, order["order_id"], "hold")
        assert api.post("/merchant/start", json={"order_id": order["order_id"]}).status_code == 202
        assert len(calls) == 1
        assert api.get(f"/merchant/orders/{order['order_id']}/status").json()["status"] == "held"


def test_failed_order_rerun_gets_a_fresh_idempotency_key(hosted, fake) -> None:
    make, calls, _state = hosted
    with make(render_api_key="rnd_test", risk_workflow_task="quietcheck-risk/process_order") as api:
        order = create(api)
        fake.approve(order["paypal_order_id"], "ana@example.com", ADDRESS)
        with api.app.state.engine.begin() as conn:
            conn.execute(update(orders).values(status="failed"))
        for _ in range(3):  # repeated calls for one failure: one key, one recorded run
            api.post("/merchant/start", json={"order_id": order["order_id"]})
        keys = {c["idempotencyKey"] for c in calls}
        assert len(keys) == 1 and keys.pop().startswith(f"{order['order_id']}:rerun:20")
        assert event_types(api, order["order_id"]).count("risk_agent.started") == 1


# AP3 keys from env vars (hosted disks are wiped on deploy)


def test_keypair_from_seed_base64_and_hex(tmp_path) -> None:
    raw = bytes(range(32))
    from_b64 = keypair_from_seed(base64.b64encode(raw).decode())
    from_hex = keypair_from_seed(raw.hex())
    assert from_b64 == from_hex
    assert from_b64[0] == raw and len(from_b64[1]) == 32
    for bad in ("not-base64!", base64.b64encode(b"short").decode(), "ab" * 31):
        with pytest.raises(ValueError):
            keypair_from_seed(bad)
    # The env key wins over the file; no file is written.
    assert load_keypair(tmp_path / "key.json", raw.hex()) == from_hex
    assert not (tmp_path / "key.json").exists()


# Hosted URLs


def test_settings_from_render_env(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith(("MEMBER_", "INITIATOR_", "RENDER", "RISK_")):
            monkeypatch.delenv(name)
    local = Settings.from_env()
    assert local.member_urls["A"] == "http://127.0.0.1:8101"
    assert (local.initiator_host, local.initiator_url) == ("127.0.0.1", "")

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "quietcheck-backend-ab12")
    monkeypatch.setenv("MEMBER_A_HOST", "quietcheck-member-a-cd34")
    monkeypatch.setenv("MEMBER_B_URL", "http://override:9999/")
    hosted = Settings.from_env()
    assert hosted.member_urls == {
        "A": "http://quietcheck-member-a-cd34:8101",
        "B": "http://override:9999",
        "C": "http://127.0.0.1:8103",
    }
    assert hosted.initiator_host == "0.0.0.0"
    assert hosted.initiator_url == "http://quietcheck-backend-ab12:8100"
    assert hosted.risk_workflow_task == ""
    monkeypatch.setenv("INITIATOR_URL", "http://elsewhere:8100")
    monkeypatch.setenv("RISK_WORKFLOW_SLUG", "quietcheck-risk-x1")
    again = Settings.from_env()
    assert again.initiator_url == "http://elsewhere:8100"
    assert again.risk_workflow_task == "quietcheck-risk-x1/process_order"
