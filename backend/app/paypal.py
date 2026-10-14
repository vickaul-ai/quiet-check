"""PayPal REST client (sandbox): OAuth token cache, Orders v2, Payments v2, Disputes v1, webhook
verification.

The Agent Toolkit has no authorise or void tool, so this calls the REST endpoints directly.
Every POST that moves money sends `PayPal-Request-Id` (docs/contracts.md, Idempotency).
"""

import asyncio
import json
import logging
import time
from collections.abc import Mapping

import httpx

logger = logging.getLogger("quietcheck.paypal")

# Refresh the cached token this many seconds before PayPal says it expires.
TOKEN_MARGIN_S = 60

WEBHOOK_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


class PayPalError(Exception):
    """PayPal returned an error, or couldn't be reached. Maps to `502 paypal_error`."""

    def __init__(
        self,
        status: int | None,
        name: str,
        message: str,
        debug_id: str | None = None,
        issue: str | None = None,
    ):
        super().__init__(f"{name}: {message}")
        self.status = status
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.issue = issue

    @property
    def details(self) -> dict:
        return {"name": self.name, "debug_id": self.debug_id, "issue": self.issue}

    @classmethod
    def from_response(cls, response: httpx.Response) -> "PayPalError":
        try:
            body = response.json()
        except ValueError:
            body = {}
        details = body.get("details") or [{}]
        return cls(
            response.status_code,
            body.get("name") or body.get("error") or f"HTTP_{response.status_code}",
            body.get("message") or body.get("error_description") or response.reason_phrase,
            body.get("debug_id") or response.headers.get("paypal-debug-id"),
            details[0].get("issue"),
        )


class PayPalClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout_s)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires:
                return self._token
            if not self._client_id or not self._client_secret:
                raise PayPalError(None, "MISSING_CREDENTIALS", "PayPal credentials are not set.")
            try:
                response = await self._http.post(
                    "/v1/oauth2/token",
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                )
            except httpx.HTTPError as exc:
                raise PayPalError(None, "CONNECTION_ERROR", type(exc).__name__) from None
            if response.status_code != 200:
                raise PayPalError.from_response(response)
            body = response.json()
            self._token = body["access_token"]
            self._token_expires = time.monotonic() + int(body["expires_in"]) - TOKEN_MARGIN_S
            return self._token

    def _drop_token(self) -> None:
        self._token = None
        self._token_expires = 0.0

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        content: str | None = None,
        files: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Send an authenticated call; returns the JSON body ({} for 204). On 401 the token is
        refreshed once (it may have been revoked early). With `files`, the body is
        multipart/form-data and httpx sets the Content-Type (with its boundary)."""
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {await self._access_token()}"}
            if files is None:
                headers["Content-Type"] = "application/json"
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                response = await self._http.request(
                    method,
                    path,
                    headers=headers,
                    json=json_body,
                    content=content if json_body is None else None,
                    files=files,
                )
            except httpx.HTTPError as exc:
                raise PayPalError(None, "CONNECTION_ERROR", type(exc).__name__) from None
            if response.status_code == 401 and attempt == 1:
                self._drop_token()
                continue
            if response.status_code >= 400:
                error = PayPalError.from_response(response)
                logger.warning(
                    "PayPal %s %s -> %s %s (debug_id %s)",
                    method,
                    path,
                    response.status_code,
                    error.name,
                    error.debug_id,
                )
                raise error
            return response.json() if response.content else {}
        raise AssertionError("unreachable")

    # Orders v2

    async def create_order(self, body: dict, request_id: str) -> dict:
        return await self.request(
            "POST", "/v2/checkout/orders", json_body=body, request_id=request_id
        )

    async def get_order(self, paypal_order_id: str) -> dict:
        return await self.request("GET", f"/v2/checkout/orders/{paypal_order_id}")

    async def authorize_order(self, paypal_order_id: str, request_id: str) -> dict:
        return await self.request(
            "POST",
            f"/v2/checkout/orders/{paypal_order_id}/authorize",
            json_body={},
            request_id=request_id,
        )

    # Payments v2

    async def capture_authorization(self, authorization_id: str, request_id: str) -> dict:
        """Capture the full authorised amount (no `amount`) as the final capture."""
        return await self.request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/capture",
            json_body={"final_capture": True},
            request_id=request_id,
        )

    async def void_authorization(self, authorization_id: str, request_id: str) -> dict:
        """Returns {} (PayPal answers 204 No Content)."""
        return await self.request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/void",
            json_body={},
            request_id=request_id,
        )

    async def reauthorize_authorization(self, authorization_id: str, request_id: str) -> dict:
        """After the 3-day honour period (and within the 29-day hold), reauthorise the full
        amount. PayPal returns a new authorisation ID with a fresh honour period. Not exposed
        as an endpoint yet (D3 decision); the sandbox can't move the clock to test it live."""
        return await self.request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/reauthorize",
            json_body={},
            request_id=request_id,
        )

    # Disputes v1 (D8)

    async def get_dispute(self, dispute_id: str) -> dict:
        return await self.request("GET", f"/v1/customer/disputes/{dispute_id}")

    async def list_disputes(self, page_size: int = 10) -> dict:
        return await self.request("GET", f"/v1/customer/disputes?page_size={page_size}")

    async def provide_evidence(
        self, dispute_id: str, notes: str, request_id: str, evidence_type: str = "OTHER"
    ) -> dict:
        """Notes-only evidence of the type PayPal asked for (default OTHER; up to 2000
        characters). Format confirmed live 2026-10-14 (PayPal parsed it). PayPal takes this as
        multipart/form-data with the JSON in a part named `input` (`application/json`), as in
        its Disputes API guide's sample; documents would be further file parts. PayPal accepts
        it only while the dispute is WAITING_FOR_SELLER_RESPONSE and shows a provide-evidence
        link. Returns PayPal's links."""
        evidence = {"evidences": [{"evidence_type": evidence_type, "notes": notes}]}
        return await self.request(
            "POST",
            f"/v1/customer/disputes/{dispute_id}/provide-evidence",
            files={"input": (None, json.dumps(evidence), "application/json")},
            request_id=request_id,
        )

    async def require_evidence(self, dispute_id: str, action: str = "SELLER_EVIDENCE") -> dict:
        """Sandbox only: move an UNDER_REVIEW dispute to WAITING_FOR_SELLER_RESPONSE
        (SELLER_EVIDENCE) or WAITING_FOR_BUYER_RESPONSE (BUYER_EVIDENCE). Used only by
        scripts/dispute_sandbox.py, never by the app."""
        return await self.request(
            "POST",
            f"/v1/customer/disputes/{dispute_id}/require-evidence",
            json_body={"action": action},
        )

    async def escalate_dispute(self, dispute_id: str, note: str) -> dict:
        """Escalate an INQUIRY-stage dispute to a PayPal claim. Used only by
        scripts/dispute_sandbox.py, to get a sandbox inquiry to UNDER_REVIEW."""
        return await self.request(
            "POST",
            f"/v1/customer/disputes/{dispute_id}/escalate",
            json_body={"note": note},
        )

    # Webhooks

    async def verify_webhook_signature(
        self, webhook_id: str, headers: Mapping[str, str], raw_body: bytes
    ) -> bool:
        """`POST /v1/notifications/verify-webhook-signature`. The event goes in as the raw
        bytes PayPal sent (spliced into the JSON), not re-serialised, so the signed content
        is exactly what PayPal signed. False if any header is missing."""
        meta = {key: headers.get(header) for key, header in WEBHOOK_HEADERS.items()}
        if not webhook_id or not all(meta.values()):
            return False
        meta["webhook_id"] = webhook_id
        content = json.dumps(meta)[:-1] + ', "webhook_event": ' + raw_body.decode("utf-8") + "}"
        result = await self.request(
            "POST", "/v1/notifications/verify-webhook-signature", content=content
        )
        return result.get("verification_status") == "SUCCESS"
