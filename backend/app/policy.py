"""The deterministic decision policy (docs/brief.md, "Decision policy"; ADR 0002).

The LLM never decides: /merchant/decide recomputes this from the stored receipts and rejects any
other decision with `409 policy_mismatch`.
"""

from collections.abc import Iterable

PAYPAL_ACTIONS = {"clean": "authorize_capture", "hold": "authorize", "block": "none"}
# The order status each decision ends in once its PayPal action has run.
STATUSES = {"clean": "captured", "hold": "held", "block": "blocked"}


def decide(hits: Iterable[dict]) -> str:
    """`hits` are the check's (member, field, is_match) results. A hit is one matching pair."""
    matched = [(h["member"], h["field"]) for h in hits if h["is_match"]]
    fields = {field for _member, field in matched}
    # The email-and-device clause is implied by hit_count >= 2 today; it is checked anyway so
    # a later change to hit counting can't quietly drop it.
    if len(matched) >= 2 or {"email", "device_id"} <= fields:
        return "block"
    if len(matched) == 1:
        return "hold"
    return "clean"
