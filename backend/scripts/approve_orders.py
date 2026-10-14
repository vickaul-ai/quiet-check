"""Create sandbox orders, wait while you approve them in the browser, and save them.

    cd backend && uv run python scripts/approve_orders.py [--count 2]

Needs: `make dev-backend` stopped (this serves the backend on :8000 itself, so the
CHECKOUT.ORDER.APPROVED webhook lands in the live DB) and `ngrok http 8000` running with the
sandbox webhook pointing at it. Approve each link as a sandbox buyer (www.sandbox.paypal.com).
Saved orders go to backend/var/live/approved_orders.json; `pytest -m live` uses two (one ends
in capture, one in void) and `scripts/gate_a.py` uses one. Use them soon after approval.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.paypal import PayPalClient, PayPalError  # noqa: E402
from tests import live  # noqa: E402

POLL_S = 3
APPROVAL_TIMEOUT_S = 15 * 60


async def run(count: int, settings, app) -> int:
    paypal = PayPalClient(
        settings.paypal_base_url, settings.paypal_client_id, settings.paypal_client_secret
    )
    created = []
    async with httpx.AsyncClient(base_url=live.BASE_URL, timeout=60) as backend:
        for i in range(count):
            response = await backend.post(
                "/payments/orders",
                json={
                    "items": [{"sku": "backpack-blue-m", "quantity": 1}],
                    "device_id": f"dev-live-{int(time.time())}-{i}",
                },
            )
            if response.status_code != 201:
                print(f"create failed: {response.status_code} {response.text}", file=sys.stderr)
                return 1
            order = response.json()
            paypal_order = await paypal.get_order(order["paypal_order_id"])
            link = next(x["href"] for x in paypal_order["links"] if x["rel"] == "approve")
            created.append(order)
            print(f"{i + 1}. {order['order_id']} ({order['amount']['value']} USD)\n   {link}")

    print("\nApprove each link as a sandbox buyer. Waiting (Ctrl-C to stop)...")
    pending = {o["order_id"]: o for o in created}
    deadline = time.monotonic() + APPROVAL_TIMEOUT_S
    while pending and time.monotonic() < deadline:
        for order_id, order in list(pending.items()):
            try:
                paypal_order = await paypal.get_order(order["paypal_order_id"])
            except PayPalError as exc:  # the sandbox sometimes answers 503; keep polling
                print(f"   (PayPal {exc.name} while polling {order_id}; retrying)")
                continue
            if paypal_order["status"] == "APPROVED":
                del pending[order_id]
                live.save_order(order_id, order["paypal_order_id"])
                address = (paypal_order["purchase_units"][0].get("shipping") or {}).get("address")
                print(f"approved and saved: {order_id}")
                print(f"   shipping.address keys: {sorted(address or {})}")
        await asyncio.sleep(POLL_S)
    await paypal.aclose()

    approved = [o["order_id"] for o in created if o["order_id"] not in pending]
    if approved:
        print("\nWaiting up to 60 s for the CHECKOUT.ORDER.APPROVED webhooks...")
        for order_id in approved:
            seen = live.wait_for_webhooks(
                app.state.engine, order_id, {"CHECKOUT.ORDER.APPROVED"}, timeout_s=60
            )
            state = {True: "verified", False: "UNVERIFIED"}.get(
                seen.get("CHECKOUT.ORDER.APPROVED"), "not received"
            )
            print(f"   {order_id}: webhook {state}")
    print(f"\nSaved orders now: {len(live.load_saved())} in {live.SAVED_ORDERS}")
    return 1 if pending else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=2)
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show progress when piped too
    live.log_to(live.LIVE_DIR / "logs" / "approve_orders.log")
    tunnel = live.ngrok_url()
    missing = f"NO TUNNEL to :{live.PORT} found; webhooks will not arrive"
    print(f"ngrok: {tunnel or missing}")
    settings = live.live_settings()
    with live.serve(settings) as app:
        return asyncio.run(run(args.count, settings, app))


if __name__ == "__main__":
    sys.exit(main())
