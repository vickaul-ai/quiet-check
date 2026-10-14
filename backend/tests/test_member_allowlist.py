"""A member refuses intents from initiators not on its allowlist, before any network fetch."""

import asyncio

from ap3.a2a.wire import ProtocolEnvelope

from app.consortium.member import build_member
from tests.harness import free_port, write_lists


def forged_envelope(initiator_url: str, receiver_url: str) -> ProtocolEnvelope:
    return ProtocolEnvelope(
        operation="protocol.psi.sanction.v1",
        phase="init",
        session_id="a" * 64,
        payload={"x": 1},
        privacy_intent={"participants": [initiator_url, receiver_url], "signature": "forged"},
    )


def test_forged_initiator_url_is_refused_without_fetching(tmp_path) -> None:
    write_lists(tmp_path, {}, filler_per_member=3)
    port = free_port()
    agent = build_member("A", port, tmp_path, allowed_initiator_urls=["http://127.0.0.1:8100"])
    fetched: list[str] = []

    async def no_fetch(url, **_kw):
        fetched.append(url)
        raise AssertionError("must not fetch")

    agent._core._peer_client.resolve_peer = no_fetch
    for target in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8000"):
        reply = asyncio.run(
            agent.handle_envelope(forged_envelope(target, f"http://127.0.0.1:{port}"))
        )
        assert reply.error["error_code"] == "INITIATOR_NOT_ALLOWED"
    assert fetched == []


def test_allowlist_matches_normalised_urls(tmp_path) -> None:
    write_lists(tmp_path, {}, filler_per_member=3)
    agent = build_member(
        "B", free_port(), tmp_path, allowed_initiator_urls=["http://127.0.0.1:8100"]
    )
    allowed = forged_envelope("HTTP://127.0.0.1:8100/", "http://127.0.0.1:1")
    assert agent.initiator_allowed(allowed)
    assert not agent.initiator_allowed(forged_envelope("http://127.0.0.1:8101", "http://x"))
    bad = forged_envelope("http://127.0.0.1:8100", "http://x")
    bad.privacy_intent["participants"] = "http://127.0.0.1:8100"
    assert not agent.initiator_allowed(bad)
