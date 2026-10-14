"""D4 + D7 live acceptance: the three personas through the PayPal sandbox, driven by webhooks.

    make seed && make live-personas

Needs: the seeded member lists (`make seed`), `make dev` stopped (this serves the backend on
:8000 and starts its own members on spare ports, reading the same lists), `ngrok http 8000`
running with the sandbox webhook pointing at it, and PERSONA_*_EMAIL in .env.

It creates one order per persona with that persona's device ID and prints the approval links.
Approve each one logged in as that persona's sandbox buyer. Nothing else calls the risk agent:
the CHECKOUT.ORDER.APPROVED webhook alone must take each order to its decision (Ana captured,
Ben held, Cy blocked). Ben's order stays held for a reviewer. Prints IDs, hit counts and
rationales only; no emails, addresses or hashes. Logs go to backend/var/live/logs/.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.config import MEMBERS, member_data_dir  # noqa: E402
from app.paypal import PayPalClient  # noqa: E402
from app.personas import load_personas, persona_emails_from_env  # noqa: E402
from tests import live  # noqa: E402

APPROVAL_TIMEOUT_S = 15 * 60
FINAL = {"captured", "held", "blocked", "voided", "failed"}
WEBHOOKS = {
    "captured": {"PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"},
    "held": {"PAYMENT.AUTHORIZATION.CREATED"},
    "blocked": set(),
}


async def approval_link(settings, paypal_order_id: str) -> str:
    paypal = PayPalClient(
        settings.paypal_base_url, settings.paypal_client_id, settings.paypal_client_secret
    )
    try:
        order = await paypal.get_order(paypal_order_id)
    finally:
        await paypal.aclose()
    return next(x["href"] for x in order["links"] if x["rel"] == "approve")


def run(app, client: httpx.Client, settings, emails: dict[str, str]) -> int:
    created = []
    for persona in load_personas():
        response = client.post(
            "/payments/orders",
            json={
                "items": [{"sku": "backpack-blue-m", "quantity": 1}],
                "device_id": persona["device_id"],
            },
        )
        if response.status_code != 201:
            print(f"create failed: {response.status_code} {response.text}", file=sys.stderr)
            return 1
        order = response.json()
        link = asyncio.run(approval_link(settings, order["paypal_order_id"]))
        created.append((persona, order))
        who = f"log in as the {persona['email_env']} buyer"
        print(f"{persona['name']} ({order['order_id']}): {who}")
        print(f"   {link}")

    print("\nApprove each link as its persona. Waiting for the webhook-driven decisions...")
    deadline = time.monotonic() + APPROVAL_TIMEOUT_S
    passed = True
    for persona, order in created:
        remaining = max(deadline - time.monotonic(), 1)
        status = live.wait_for_status(app.state.engine, order["order_id"], FINAL, remaining)
        detail = client.get(f"/merchant/orders/{order['order_id']}").json()
        ok = status == persona["status"]
        right_buyer = (detail["payer_email"] or "").lower() == emails[persona["key"]].lower()
        print(f"\n{persona['name']}: {status} (expected {persona['status']})")
        if not right_buyer:
            print(f"   WRONG BUYER: approved by an account other than {persona['email_env']}")
            ok = False
        if detail["check"]:
            hits = [f"{h['member']}/{h['field']}" for h in detail["check"]["hits"] if h["is_match"]]
            verified = sum(r["verified"] for r in detail["receipts"])
            print(f"   hit_count={detail['hit_count']} {hits}; {verified}/9 receipts verified")
        if detail["decision"]:
            decision = detail["decision"]
            print(f"   decision {decision['decision_id']}: {decision['decision']}")
            print(f"   rationale ({decision['rationale_source']}): {decision['rationale']}")
        print(
            f"   PayPal order {detail['paypal_order_id']}, authorization "
            f"{detail['authorization_id']}, capture {detail['capture_id']}"
        )
        expected = WEBHOOKS.get(persona["status"], set()) | {"CHECKOUT.ORDER.APPROVED"}
        seen = live.wait_for_webhooks(app.state.engine, order["order_id"], expected, 120)
        for event_type in sorted(expected):
            state = {True: "verified", False: "UNVERIFIED"}.get(seen.get(event_type), "missing")
            print(f"   {event_type}: {state}")
        ok = ok and set(seen) == expected and all(seen.values())
        passed = passed and ok
    print("\nAll three personas passed." if passed else "\nPersona run FAILED.")
    return 0 if passed else 1


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    missing = [m for m in MEMBERS if not (member_data_dir() / m / "list.csv").exists()]
    if missing:
        names = ", ".join(missing)
        print(f"No list for member(s) {names}: run `make seed` first.", file=sys.stderr)
        return 1
    try:
        emails = persona_emails_from_env()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    live.log_to(live.LIVE_DIR / "logs" / "live_personas.log")
    print(f"ngrok: {live.ngrok_url() or 'NO TUNNEL: webhooks will not arrive'}")
    log_dir = live.LIVE_DIR / "personas" / "logs"
    with live.consortium(None, log_dir) as overrides:
        settings = live.live_settings(**overrides)
        with live.serve(settings) as app, httpx.Client(base_url=live.BASE_URL, timeout=120) as c:
            return run(app, c, settings, emails)


if __name__ == "__main__":
    sys.exit(main())
