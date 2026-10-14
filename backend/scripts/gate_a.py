"""Gate A: one saved sandbox order through the risk agent -> capture, or hold -> reviewer void.

    cd backend && uv run python scripts/gate_a.py --end capture   # clean: 0 hits, policy capture
    cd backend && uv run python scripts/gate_a.py --end void      # hold: 1 hit, reviewer void

Takes one order saved by `scripts/approve_orders.py`. Same setup as the live tests: `make
dev-backend` stopped, `ngrok http 8000` running. It starts Members A-C with synthetic lists
(about 300 filler entries each, under backend/var/live/gate-a/). For `--end void` it also puts
the buyer's email hash on Member B's list, so the check really finds one hit (a Ben-style hold).
Then `POST /merchant/process` runs the real risk agent (check, policy, rationale, decide and the
PayPal action); for a hold, the script plays the reviewer and voids.
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

from app.db import make_engine  # noqa: E402
from app.identifiers import identifier_hash  # noqa: E402
from app.payments import fetch_payer_details  # noqa: E402
from app.paypal import PayPalClient  # noqa: E402
from tests import live  # noqa: E402

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

    seeded = {}
    if args.end == "void":
        step("Read the payer email from PayPal to seed it on Member B's list")
        base = live.live_settings()
        engine = make_engine(base.database_url)
        paypal = PayPalClient(
            base.paypal_base_url, base.paypal_client_id, base.paypal_client_secret
        )
        email = asyncio.run(_fetch(engine, paypal, order_id))["payer"]["email_address"]
        engine.dispose()
        seeded = {"B": [identifier_hash("email", email)]}

    shutil.rmtree(GATE_DIR, ignore_errors=True)
    with live.consortium(
        GATE_DIR / "members", GATE_DIR / "logs", seeded, filler=FILLER_PER_MEMBER
    ) as overrides:
        settings = live.live_settings(**overrides)
        with live.serve(settings) as app, httpx.Client(base_url=live.BASE_URL, timeout=120) as c:
            return run(app, c, order_id, args.end)


async def _fetch(engine, paypal: PayPalClient, order_id: str) -> dict:
    try:
        return await fetch_payer_details(engine, paypal, order_id)
    finally:
        await paypal.aclose()


def run(app, client: httpx.Client, order_id: str, end: str) -> int:
    engine = app.state.engine
    step("POST /merchant/process (9 PSI sessions, policy, rationale, decide, PayPal action)")
    result = client.post("/merchant/process", json={"order_id": order_id})
    if result.status_code != 200:
        print(f"   process failed: {result.status_code} {result.text}", file=sys.stderr)
        return 1
    body = result.json()
    detail = client.get(f"/merchant/orders/{order_id}").json()
    hits = [f"{h['member']}/{h['field']}" for h in detail["check"]["hits"] if h["is_match"]]
    verified = sum(r["verified"] for r in detail["receipts"])
    print(f"   hit_count={detail['hit_count']} {hits}; {verified}/9 receipts verified")
    print(f"   decision={body['decision']} status={body['status']}")
    decision = detail["decision"]
    print(f"   rationale ({decision['rationale_source']}): {decision['rationale']}")
    expected = "captured" if end == "capture" else "held"
    if body["status"] != expected:
        print(f"   expected {expected}, got {body['status']}")
        return 1
    print(
        f"   {detail['authorization_id']}, honour period ends {detail['honor_period_ends_at']}, "
        f"capture {detail['capture_id']}"
    )

    if end == "capture":
        webhooks = {"PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"}
    else:
        step("POST void (actor: reviewer)")
        auth_path = f"/payments/authorizations/{detail['authorization_id']}"
        voided = client.post(f"{auth_path}/void", json={"actor": "reviewer", "note": "Gate A"})
        if voided.status_code != 200:
            print(f"   void failed: {voided.status_code} {voided.text}", file=sys.stderr)
            return 1
        print(f"   order status: {voided.json()['status']}")
        webhooks = {"PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED"}

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
