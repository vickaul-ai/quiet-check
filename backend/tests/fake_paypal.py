"""An in-memory PayPal sandbox for CI: an httpx MockTransport that keeps order, authorisation and
dispute state, honours PayPal-Request-Id, and verifies webhooks whose signature header is "good".

Response shapes follow PayPal's published Orders v2 / Payments v2 / Webhooks v1 schemas
(developer.paypal.com/api/*/schema.json, checked 2026-10-13) and Disputes v1 (checked
2026-10-14; provide-evidence as multipart/form-data with an `input` JSON part, per PayPal's
Disputes API guide).
"""

import email.parser
import email.policy
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
        self.disputes: dict[str, dict] = {}
        self.evidence: dict[str, list[dict]] = {}  # dispute ID -> submitted evidences
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

    def open_dispute(
        self,
        capture_id: str,
        reason: str = "UNAUTHORISED",
        status: str = "WAITING_FOR_SELLER_RESPONSE",
        buyer_email: str = "buyer@example.com",
        seller_transaction_id: str | None = None,
        requested_evidence: str | None = None,
    ) -> dict:
        """A buyer opens a dispute on a captured payment. The transaction carries the buyer's
        name and email, as PayPal's does, so tests can check they never reach the model."""
        auth = next(
            a for a in self.authorizations.values() if a.get("capture", {}).get("id") == capture_id
        )
        created = _now()
        dispute_id = f"PP-D-{secrets.randbelow(10**6):06d}"
        self.disputes[dispute_id] = {
            "dispute_id": dispute_id,
            "create_time": _z(created),
            "update_time": _z(created),
            "disputed_transactions": [
                {
                    "buyer_transaction_id": _id(),
                    "seller_transaction_id": seller_transaction_id or capture_id,
                    "create_time": auth["capture"]["create_time"],
                    "transaction_status": "COMPLETED",
                    "gross_amount": auth["amount"],
                    "custom": auth["custom_id"],
                    "buyer": {"name": "Test Buyer", "email": buyer_email},
                    "seller": {"merchant_id": _id(13), "name": "Quiet Check Test Shop"},
                }
            ],
            "reason": reason,
            "status": status,
            "dispute_amount": auth["amount"],
            "dispute_life_cycle_stage": "CHARGEBACK",
            "dispute_channel": "INTERNAL",
            "seller_response_due_date": _z(created + timedelta(days=10)),
            "evidences": (
                [{"evidence_type": requested_evidence, "source": "REQUESTED_FROM_SELLER"}]
                if requested_evidence
                else []
            ),
        }
        return self.disputes[dispute_id]

    def dispute_view(self, dispute: dict) -> dict:
        """GET shape: HATEOAS links depend on the status, as on PayPal."""
        base = f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute['dispute_id']}"
        links = [{"href": base, "rel": "self", "method": "GET"}]
        if dispute["status"] == "WAITING_FOR_SELLER_RESPONSE":
            links.append(
                {"href": f"{base}/provide-evidence", "rel": "provide_evidence", "method": "POST"}
            )
        if dispute["status"] == "UNDER_REVIEW":
            links.append(
                {"href": f"{base}/require-evidence", "rel": "require_evidence", "method": "POST"}
            )
        return {**dispute, "links": links}

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
        multipart = request.headers.get("content-type", "").startswith("multipart/")
        body = json.loads(request.content) if request.content and not multipart else {}
        if parts[:3] == ["v1", "customer", "disputes"]:
            return self._dispute_route(request, parts[3:], body)
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

    def _dispute_route(self, request: httpx.Request, rest: list[str], body: dict):
        if request.method == "GET" and not rest:
            items = [
                {k: d[k] for k in ("dispute_id", "reason", "status", "dispute_amount")}
                for d in self.disputes.values()
            ]
            return httpx.Response(200, json={"items": items})
        dispute = self.disputes.get(rest[0]) if rest else None
        if dispute is None:
            return _error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
        if request.method == "GET" and len(rest) == 1:
            return httpx.Response(200, json=self.dispute_view(dispute))
        action = rest[1] if len(rest) == 2 and request.method == "POST" else None
        if action == "provide-evidence":
            evidences = _evidence_input(request)
            if evidences is None:
                return _error(400, "INVALID_REQUEST", "INVALID_INPUT")
            if dispute["status"] != "WAITING_FOR_SELLER_RESPONSE":
                return _error(422, "UNPROCESSABLE_ENTITY", "NOT_ALLOWED")
            asked = [e["evidence_type"] for e in dispute["evidences"]]
            if asked and any(e["evidence_type"] not in asked for e in evidences):
                return _error(400, "INVALID_REQUEST", "EVIDENCE_TYPE_IS_NOT_ALLOWED")
            self.evidence.setdefault(dispute["dispute_id"], []).extend(evidences)
            dispute["status"] = "UNDER_REVIEW"
            return httpx.Response(200, json={"links": self.dispute_view(dispute)["links"][:1]})
        if action == "require-evidence":
            if dispute["status"] != "UNDER_REVIEW":
                return _error(422, "UNPROCESSABLE_ENTITY", "NOT_ALLOWED")
            target = {
                "SELLER_EVIDENCE": "WAITING_FOR_SELLER_RESPONSE",
                "BUYER_EVIDENCE": "WAITING_FOR_BUYER_RESPONSE",
            }.get(body.get("action"))
            if target is None:
                return _error(400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE")
            dispute["status"] = target
            return httpx.Response(200, json={"links": self.dispute_view(dispute)["links"][:1]})
        return _error(404, "NOT_FOUND")


def _evidence_input(request: httpx.Request) -> list[dict] | None:
    """The `input` part of a provide-evidence multipart/form-data body: JSON
    `{"evidences": [{"evidence_type", "notes"}]}`, notes 1-2000 characters. None if malformed."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return None
    raw = f"Content-Type: {content_type}\r\n\r\n".encode() + request.content
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") != "input":
            continue
        if part.get_content_type() != "application/json":
            return None
        try:
            evidences = json.loads(part.get_payload(decode=True))["evidences"]
        except (ValueError, KeyError, TypeError):
            return None
        ok = (
            isinstance(evidences, list)
            and evidences
            and all(
                isinstance(e, dict)
                and e.get("evidence_type")
                and isinstance(e.get("notes"), str)
                and 1 <= len(e["notes"]) <= 2000
                for e in evidences
            )
        )
        return evidences if ok else None
    return None


def dispute_webhook(dispute: dict, event_type: str = "CUSTOMER.DISPUTE.CREATED") -> dict:
    """The webhook for a dispute. Its resource is the dispute without links (PayPal's can be
    partial, so the backend reads the dispute again)."""
    return webhook_event(event_type, {k: v for k, v in dispute.items() if k != "links"})


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
