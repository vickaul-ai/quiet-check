"""Gate A: one saved sandbox order through risk check -> authorise -> capture or void.

    cd backend && uv run python scripts/gate_a.py --end capture   # clean: 0 hits, policy capture
    cd backend && uv run python scripts/gate_a.py --end void      # hold: 1 hit, reviewer void

Takes one order saved by `scripts/approve_orders.py`. Same setup as the live tests: `make
dev-backend` stopped, `ngrok http 8000` running. It starts Members A-C with synthetic lists
(about 300 filler entries each, under backend/var/live/gate-a/). For `--end void` it also puts
the buyer's email hash on Member B's list, so the check really finds one hit (a Ben-style hold).
D4 doesn't exist yet, so the tests' stand-ins move the order to `checking`, record the
decision, and mark it `held`; the decision follows the hit count (0 clean, 1 hold).
Nothing identifying is printed or logged; logs go to backend/var/live/logs/gate_a.log.
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.config import MEMBERS  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.identifiers import identifier_hash  # noqa: E402
from app.payments import fetch_payer_details  # noqa: E402
from app.paypal import PayPalClient  # noqa: E402
from tests import live  # noqa: E402
from tests.harness import MemberProcess, free_port, write_lists  # noqa: E402
from tests.standins import mark_held, record_decision, start_checking  # noqa: E402

GATE_DIR = live.LIVE_DIR / "gate-a"
FILLER_PER_MEMBER = 300


def step(text: str) -> None:
    print(f"-> {text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--end", choices=("capture", "void"), default="capture")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show progress when piped too
    live.log_to(live.LIVE_DIR / "logs" / "gate_a.log")

    saved = live.take_order()
    if saved is None:
        print("No saved orders. Run scripts/approve_orders.py --count 1 first.", file=sys.stderr)
        return 1
    order_id = saved["order_id"]
    tunnel = live.ngrok_url()
    print(f"Gate A on {order_id} (ends in {args.end}); ngrok: {tunnel or 'none, no webhooks'}")

    base = live.live_settings()
    initiator_port = free_port()
    engine = make_engine(base.database_url)
    step("D4 stand-in: order -> checking; read payer email and address from PayPal")
    start_checking(engine, order_id)
    paypal = PayPalClient(base.paypal_base_url, base.paypal_client_id, base.paypal_client_secret)
    email = asyncio.run(_fetch(engine, paypal, order_id))["payer"]["email_address"]
    engine.dispose()

    seeded = {"B": [identifier_hash("email", email)]} if args.end == "void" else {}
    shutil.rmtree(GATE_DIR, ignore_errors=True)
    write_lists(GATE_DIR / "members", seeded, filler_per_member=FILLER_PER_MEMBER)
    initiator_url = f"http://127.0.0.1:{initiator_port}"
    members = {
        m: MemberProcess(m, free_port(), GATE_DIR / "members", GATE_DIR / "logs", [initiator_url])
        for m in MEMBERS
    }
    try:
        for proc in members.values():
            proc.wait_ready()
        settings = live.live_settings(
            start_initiator=True,
            initiator_port=initiator_port,
            initiator_key_path=GATE_DIR / "initiator" / "key.json",
            member_urls={m: p.url for m, p in members.items()},
        )
        with live.serve(settings) as app, httpx.Client(base_url=live.BASE_URL, timeout=120) as c:
            return run(app, c, order_id, args.end)
    finally:
        for proc in members.values():
            proc.stop()


async def _fetch(engine, paypal: PayPalClient, order_id: str) -> dict:
    try:
        return await fetch_payer_details(engine, paypal, order_id)
    finally:
        await paypal.aclose()


def run(app, client: httpx.Client, order_id: str, end: str) -> int:
    engine = app.state.engine
    step("POST /risk/check (9 PSI sessions against Members A-C)")
    check = client.post("/risk/check", json={"order_id": order_id})
    if check.status_code != 200:
        print(f"   risk check failed: {check.status_code} {check.text}", file=sys.stderr)
        return 1
    body = check.json()
    hits = [f"{h['member']}/{h['field']}" for h in body["hits"] if h["is_match"]]
    verified = sum(r["verified"] for r in body["receipts"])
    print(f"   hit_count={body['hit_count']} {hits}; {verified}/9 receipts verified")
    decision = {0: "clean", 1: "hold"}.get(body["hit_count"])
    expected = "clean" if end == "capture" else "hold"
    if decision != expected:
        print(f"   expected a {expected} decision, got hit_count {body['hit_count']}")
        return 1

    step(f"D4 stand-in: record a {decision} decision")
    record_decision(engine, order_id, decision, check_id=body["check_id"])
    step("POST /payments/orders/{id}/authorize")
    auth = client.post(f"/payments/orders/{order_id}/authorize", json={})
    if auth.status_code != 200:
        print(f"   authorize failed: {auth.status_code} {auth.text}", file=sys.stderr)
        return 1
    auth = auth.json()
    print(
        f"   {auth['authorization_id']} {auth['authorization_status']}, "
        f"honour period ends {auth['honor_period_ends_at']}, expires {auth['expires_at']}"
    )

    auth_path = f"/payments/authorizations/{auth['authorization_id']}"
    if end == "capture":
        step("POST capture (actor: policy)")
        result = client.post(f"{auth_path}/capture", json={"actor": "policy"})
        webhooks = {"PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"}
    else:
        step("D4 stand-in: order -> held; then POST void (actor: reviewer)")
        mark_held(engine, order_id)
        result = client.post(f"{auth_path}/void", json={"actor": "reviewer", "note": "Gate A"})
        webhooks = {"PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED"}
    if result.status_code not in (200, 201):
        print(f"   {end} failed: {result.status_code} {result.text}", file=sys.stderr)
        return 1
    print(f"   order status: {result.json()['status']}")

    step("Waiting for PayPal webhooks (up to 120 s)")
    seen = live.wait_for_webhooks(engine, order_id, webhooks, timeout_s=120)
    for event_type in sorted(webhooks):
        state = {True: "verified", False: "UNVERIFIED"}.get(seen.get(event_type), "not received")
        print(f"   {event_type}: {state}")
    passed = set(seen) == webhooks and all(seen.values())
    print("Gate A passed." if passed else "Gate A failed: webhooks missing or unverified.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
