"""Sandbox disputes (D8): show a dispute and move it on until PayPal asks for evidence.

    cd backend && uv run python scripts/dispute_sandbox.py --list
    cd backend && uv run python scripts/dispute_sandbox.py PP-D-12345
    cd backend && uv run python scripts/dispute_sandbox.py PP-D-12345 --escalate
    cd backend && uv run python scripts/dispute_sandbox.py PP-D-12345 --require-evidence

A buyer opens the dispute in the sandbox Resolution Center on a captured order. A new dispute is
usually an INQUIRY (status OPEN or WAITING_FOR_SELLER_RESPONSE). `--escalate` turns an inquiry into
a claim (UNDER_REVIEW); `--require-evidence` calls PayPal's sandbox-only require-evidence
endpoint (`SELLER_EVIDENCE`), which moves an UNDER_REVIEW dispute to WAITING_FOR_SELLER_RESPONSE,
the one status in which the console can submit evidence. Each run prints the dispute's status,
reason, stage, amount, due date, the capture ID it disputes (our `orders.capture_id`) and which
actions PayPal offers. It never prints the buyer's name or email, or any credential.

Uses PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET from the repo-root .env (sandbox only).
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.disputes import can_provide_evidence  # noqa: E402
from app.paypal import PayPalClient, PayPalError  # noqa: E402


def show(dispute: dict) -> None:
    amount = dispute.get("dispute_amount") or {}
    rels = sorted({str(link.get("rel")) for link in dispute.get("links") or []} - {"self"})
    print(f"dispute   {dispute.get('dispute_id')}")
    print(f"status    {dispute.get('status')}")
    print(f"reason    {dispute.get('reason')}")
    print(f"stage     {dispute.get('dispute_life_cycle_stage')}")
    print(f"amount    {amount.get('value')} {amount.get('currency_code')}")
    print(f"due       {dispute.get('seller_response_due_date')}")
    for txn in dispute.get("disputed_transactions") or []:
        print(f"capture   {txn.get('seller_transaction_id')} (our order: {txn.get('custom')})")
    print(f"actions   {', '.join(rels) or '-'}")
    ready = dispute.get("status") == "WAITING_FOR_SELLER_RESPONSE" and can_provide_evidence(dispute)
    print(f"evidence  {'PayPal is asking for it: submit from the console' if ready else 'not yet'}")


async def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    paypal = PayPalClient(
        settings.paypal_base_url, settings.paypal_client_id, settings.paypal_client_secret
    )
    try:
        if args.list:
            for item in (await paypal.list_disputes()).get("items") or []:
                print(f"{item.get('dispute_id')}  {item.get('status')}  {item.get('reason')}")
            return 0
        if args.escalate:
            await paypal.escalate_dispute(args.dispute_id, "Escalated for a sandbox test.")
            print("-> escalated to a claim")
        if args.require_evidence:
            await paypal.require_evidence(args.dispute_id, "SELLER_EVIDENCE")
            print("-> require-evidence (SELLER_EVIDENCE) accepted")
        show(await paypal.get_dispute(args.dispute_id))
        return 0
    except PayPalError as exc:
        print(f"PayPal error: {exc.name} ({exc.issue or exc.message}; debug_id {exc.debug_id})")
        return 1
    finally:
        await paypal.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dispute_id", nargs="?", help="PayPal dispute ID, e.g. PP-D-12345")
    parser.add_argument("--list", action="store_true", help="list recent disputes")
    parser.add_argument("--escalate", action="store_true", help="turn an inquiry into a claim")
    parser.add_argument(
        "--require-evidence",
        action="store_true",
        help="sandbox only: UNDER_REVIEW -> WAITING_FOR_SELLER_RESPONSE",
    )
    args = parser.parse_args()
    if not args.list and not args.dispute_id:
        parser.error("give a dispute ID, or --list")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
