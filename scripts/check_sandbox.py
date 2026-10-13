# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28", "python-dotenv>=1.2"]
# ///
"""Check that the PayPal sandbox credentials in .env can fetch an OAuth token.

Run from the repo root: uv run scripts/check_sandbox.py
Prints the token's type, lifetime and app ID; never prints the token itself.
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in .env", file=sys.stderr)
        return 1

    response = httpx.post(
        f"{SANDBOX_BASE_URL}/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    if response.status_code != 200:
        body = response.json() if response.content else {}
        print(f"Token request failed: HTTP {response.status_code} {body.get('error', '')}", file=sys.stderr)
        return 1

    token = response.json()
    print(
        f"OK: {token['token_type']} token for app {token.get('app_id', '?')}, "
        f"expires in {token['expires_in']}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
