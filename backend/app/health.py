"""GET /health/deep: what the hosted demo depends on, in one request (D9 follow-up).

Behind the API token (it isn't one of the public routes in app/main.py). It reports the database,
the AP3 initiator's URL, each member (reachable, the card URL it declares next to the URL we call
it at, and whether its list matches the one we expect), and the risk workflow's task.

The list check compares each member's published commitment `data_hash` (sha256 of its sorted
list, already on its AgentCard) with `MEMBER_DATA_HASHES`, computed offline from the persona
emails with `scripts/seed.py --digests`. So a wrong persona email on a member shows up here
without the member ever answering "is X on your list?", and without any email or entry here.
"""

import asyncio

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter(tags=["health"])


def _card_facts(card: dict) -> tuple[str | None, str | None]:
    """(declared URL, commitment data_hash) from a member's AgentCard JSON."""
    interfaces = card.get("supportedInterfaces") or []
    url = interfaces[0].get("url", "").rstrip("/") if interfaces else None
    data_hash = None
    for ext in (card.get("capabilities") or {}).get("extensions") or []:
        commitments = (ext.get("params") or {}).get("commitments") or []
        if commitments:
            data_hash = commitments[0].get("data_hash")
    return url, data_hash


async def _member(client: httpx.AsyncClient, member: str, url: str, expected: str | None) -> dict:
    out: dict = {"member": member, "url": url}
    try:
        response = await client.get(f"{url}/.well-known/agent-card.json")
        response.raise_for_status()
        declared, data_hash = _card_facts(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        return out | {"ok": False, "error": type(exc).__name__}
    url_ok = declared == url
    list_ok = None if expected is None else data_hash == expected
    return out | {
        "ok": url_ok and list_ok is not False,
        "declared_url": declared,
        "url_matches": url_ok,
        # None: no expected hash configured, so the list can't be checked.
        "list_matches_expected": list_ok,
    }


@router.get("/health/deep")
async def deep_health(request: Request) -> dict:
    state = request.app.state
    settings = state.settings
    try:
        with state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = {"ok": True}
    except Exception as exc:  # any driver error: report it, don't crash the probe
        database = {"ok": False, "error": type(exc).__name__}
    async with httpx.AsyncClient(timeout=5.0) as client:
        members = await asyncio.gather(
            *(
                _member(client, m, url, settings.expected_member_hashes.get(m))
                for m, url in settings.member_urls.items()
            )
        )
    initiator = state.initiator
    workflow = {
        "configured": bool(settings.render_api_key and settings.risk_workflow_task),
        "task": settings.risk_workflow_task or None,
    }
    checks = [database["ok"], initiator is not None, *(m["ok"] for m in members)]
    return {
        "ok": all(checks),
        "database": database,
        "initiator": {"url": initiator.card_url if initiator else None},
        "members": members,
        "workflow": workflow,
    }
