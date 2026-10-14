"""An in-memory PayPal sandbox for CI: an httpx MockTransport that keeps order and authorisation
state, honours PayPal-Request-Id, and verifies webhooks whose signature header is "good".

Response shapes follow PayPal's published Orders v2 / Payments v2 / Webhooks v1 schemas
(developer.paypal.com/api/*/schema.json, checked 2026-10-13).
"""

import json
import secrets
from datetime import UTC, datetime, timedelta

import httpx

GOOD_SIGNATURE = "good"
WEBHOOK_ID = "WH-FAKE-0001"


def _id(n: int = 17) -> str:
    return secrets.token_hex(n).upper()[:n]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(status: int, name: str, issue: str | None = None) -> httpx.Response:
    body = {"name": name, "message": f"fake {name}", "debug_id": "fakedebug" + _id(6)}
    if issue:
        body["details"] = [{"issue": issue, "description": issue}]
    return httpx.Response(status, json=body, headers={"paypal-debug-id": body["debug_id"]})


class FakePayPal:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.idempotent: dict[str, tuple[int, bytes]] = {}  # PayPal-Request-Id -> response
        self.fail_next: dict[str, httpx.Response] = {}  # path suffix -> canned response
        self.expire_tokens = False
        self.transport = httpx.MockTransport(self.handle)

    # Test controls

    def approve(self, paypal_order_id: str, email: str, address: dict) -> None:
        order = self.orders[paypal_order_id]
        order["status"] = "APPROVED"
        order["payer"] = {"email_address": email, "payer_id": _id(13)}
        order["purchase_units"][0]["shipping"] = {"name": {"full_name": "Test"}, "address": address}

    def calls(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    # Transport

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            self.token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok-{self.token_calls}", "expires_in": 32400},
            )
        if self.expire_tokens or request.headers.get("authorization") != (
            f"Bearer tok-{self.token_calls}"
        ):
            self.expire_tokens = False
            return httpx.Response(401, json={"error": "invalid_token"})
        for suffix, response in list(self.fail_next.items()):
            if path.endswith(suffix):
                del self.fail_next[suffix]
                return response
        request_id = request.headers.get("paypal-request-id")
        if request_id and request_id in self.idempotent:
            status, content = self.idempotent[request_id]
            return httpx.Response(status, content=content)
        response = self._route(request, path)
        if request_id and response.status_code < 300:
            self.idempotent[request_id] = (response.status_code, response.read())
        return response

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "/v2/checkout/orders":
            return self._create(body)
        if parts[:3] == ["v2", "checkout", "orders"] and len(parts) >= 4:
            order = self.orders.get(parts[3])
            if order is None:
                return _error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
            if request.method == "GET" and len(parts) == 4:
                return httpx.Response(200, json=order)
            if request.method == "POST" and parts[4:] == ["authorize"]:
                return self._authorize(order)
        if parts[:3] == ["v2", "payments", "authorizations"] and len(parts) == 5:
            auth = self.authorizations.get(parts[3])
            if auth is None:
                return _error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
            return self._auth_action(auth, parts[4])
        if path == "/v1/notifications/verify-webhook-signature":
            ok = body.get("transmission_sig") == GOOD_SIGNATURE and body.get("webhook_id") == (
                WEBHOOK_ID
            )
            return httpx.Response(200, json={"verification_status": "SUCCESS" if ok else "FAILURE"})
        return _error(404, "NOT_FOUND")

    def _create(self, body: dict) -> httpx.Response:
        order_id = _id()
        unit = body["purchase_units"][0]
        self.orders[order_id] = {
            "id": order_id,
            "intent": body["intent"],
            "status": "CREATED",
            "purchase_units": [
                {
                    "reference_id": "default",
                    "custom_id": unit.get("custom_id"),
                    "amount": unit["amount"],
                    "items": unit.get("items", []),
                }
            ],
            "links": [
                {
                    "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                    "rel": "approve",
                    "method": "GET",
                }
            ],
        }
        order = self.orders[order_id]
        return httpx.Response(
            201, json={"id": order_id, "status": "CREATED", "links": order["links"]}
        )

    def _authorize(self, order: dict) -> httpx.Response:
        if order["status"] != "APPROVED":
            return _error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
        created = _now()
        unit = order["purchase_units"][0]
        auth = {
            "id": _id(),
            "status": "CREATED",
            "amount": {k: unit["amount"][k] for k in ("currency_code", "value")},
            "custom_id": unit["custom_id"],
            "expiration_time": _z(created + timedelta(days=29)),
            "create_time": _z(created),
            "update_time": _z(created),
            "supplementary_data": {"related_ids": {"order_id": order["id"]}},
        }
        self.authorizations[auth["id"]] = auth
        unit["payments"] = {"authorizations": [auth]}
        order["status"] = "COMPLETED"
        return httpx.Response(201, json=order)

    def _auth_action(self, auth: dict, action: str) -> httpx.Response:
        if action == "capture":
            if auth["status"] != "CREATED":
                return _error(422, "UNPROCESSABLE_ENTITY", f"AUTHORIZATION_{auth['status']}")
            auth["status"] = "CAPTURED"
            capture = {
                "id": _id(),
                "status": "COMPLETED",
                "amount": auth["amount"],
                "final_capture": True,
                "custom_id": auth["custom_id"],
                "create_time": _z(_now()),
            }
            auth["capture"] = capture
            return httpx.Response(201, json=capture)
        if action == "void":
            if auth["status"] != "CREATED":
                return _error(422, "UNPROCESSABLE_ENTITY", "PREVIOUSLY_CAPTURED")
            auth["status"] = "VOIDED"
            return httpx.Response(204)
        if action == "reauthorize":
            new = {**auth, "id": _id(), "status": "CREATED", "create_time": _z(_now())}
            self.authorizations[new["id"]] = new
            return httpx.Response(201, json=new)
        return _error(404, "NOT_FOUND")


def webhook_event(event_type: str, resource: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"WH-{_id(8)}-{_id(8)}",
        "event_version": "1.0",
        "create_time": _z(_now()),
        "resource_type": event_type.split(".")[1].lower(),
        "event_type": event_type,
        "summary": f"fake {event_type}",
        "resource": resource,
    }


def webhook_headers(signature: str = GOOD_SIGNATURE) -> dict:
    return {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-fake",
        "PAYPAL-TRANSMISSION-ID": _id(12),
        "PAYPAL-TRANSMISSION-SIG": signature,
        "PAYPAL-TRANSMISSION-TIME": _z(_now()),
    }
