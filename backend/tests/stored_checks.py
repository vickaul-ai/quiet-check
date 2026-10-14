"""Completed AP3 checks written straight to the DB, for tests that run without member processes.

The payments and webhook tests use these with the real /merchant/process: a stored completed
check is returned as is (no PSI), so the risk agent goes on to the policy, the rationale and
/merchant/decide. The receipts carry placeholder directives; only `is_match` matters here.
"""

from sqlalchemy import Engine, insert

from app.config import MEMBERS
from app.db import checks, receipts, utcnow
from app.identifiers import FIELDS
from app.ids import new_id

# (member, field) hits per decision: Ben-style and Cy-style.
HITS = {
    "clean": set(),
    "hold": {("B", "email")},
    "block": {("A", "email"), ("A", "device_id"), ("C", "email"), ("C", "device_id")},
}


def store_check(engine: Engine, order_id: str, decision: str) -> str:
    hits = HITS[decision]
    check_id = new_id("chk")
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(checks).values(
                id=check_id,
                order_id=order_id,
                status="completed",
                hit_count=len(hits),
                started_at=now,
                completed_at=now,
            )
        )
        conn.execute(
            insert(receipts),
            [
                {
                    "id": new_id("rcp"),
                    "check_id": check_id,
                    "member": member,
                    "field": field,
                    "is_match": (member, field) in hits,
                    "session_id": f"stored-{member}-{field}",
                    "intent_directives": [],
                    "result_directive": {},
                    "member_commitment": {},
                    "member_public_key": "00",
                    "verified": True,
                    "created_at": now,
                }
                for member in MEMBERS
                for field in FIELDS
            ],
        )
    return check_id
