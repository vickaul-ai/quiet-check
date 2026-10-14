"""Consortium member: one AP3 receiver (PSI) holding one private list.

Run once per member:

    uv run python -m app.consortium.member A    # :8101; B on :8102, C on :8103

Reads `<MEMBER_DATA_DIR>/<member>/list.csv` and `key.json` (default `backend/var/members/`).
The key is created on first start and kept, so the member's identity survives restarts.
The list is read once at startup; restart the member after reseeding.

A member answers only the initiators on its allowlist (`--initiator-url`, default the
backend's http://127.0.0.1:8100). The check runs before the SDK fetches the initiator's
AgentCard, so a forged intent can't make the member request any other URL.
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from ap3.a2a import PrivacyAgent
from ap3.a2a.card import normalize_url
from ap3.a2a.wire import ProtocolEnvelope
from ap3.types import DataStructure, PrivacyError
from ap3_functions import PSIOperation

from app.config import INITIATOR_PORT, MEMBER_PORTS, MEMBERS, member_data_dir
from app.consortium.common import (
    ListFileError,
    build_commitment,
    list_digest,
    load_list,
    load_or_create_keypair,
)

logger = logging.getLogger("quietcheck.member")

DEFAULT_INITIATOR_URL = f"http://127.0.0.1:{INITIATOR_PORT}"


class MemberAgent(PrivacyAgent):
    """A receiver that refuses any intent whose initiator isn't on its allowlist.

    `allow_private_initiator_urls=True` turns off the SDK's SSRF guard (ADR 0004), because our
    initiator is on localhost or a private network. The allowlist replaces that guard: an
    initiator URL comes from the unauthenticated envelope and the SDK fetches it before checking
    any signature, so it must be one we already trust.
    """

    def __init__(self, *, allowed_initiator_urls: list[str], **kwargs):
        super().__init__(**kwargs)
        self.allowed_initiators = frozenset(normalize_url(u) for u in allowed_initiator_urls)

    def initiator_allowed(self, envelope: ProtocolEnvelope) -> bool:
        intent = envelope.privacy_intent
        if intent is None:
            return True  # no URL to fetch; the SDK refuses a session opened without an intent
        participants = intent.get("participants") if isinstance(intent, dict) else None
        if not isinstance(participants, list) or not participants:
            return False
        initiator = participants[0]
        return isinstance(initiator, str) and normalize_url(initiator) in self.allowed_initiators

    async def handle_envelope(self, envelope: ProtocolEnvelope) -> ProtocolEnvelope | None:
        if not self.initiator_allowed(envelope):
            logger.warning("refusing envelope from an initiator not on the allowlist")
            error = PrivacyError(
                error_code="INITIATOR_NOT_ALLOWED",
                error_message="initiator is not on this member's allowlist",
                operation_type="PSI",
            )
            return ProtocolEnvelope(
                operation=envelope.operation,
                phase="error",
                session_id=envelope.session_id,
                payload={},
                error=error.model_dump(mode="json"),
            )
        return await super().handle_envelope(envelope)


def build_member(
    member: str,
    port: int,
    data_dir: Path,
    host: str = "127.0.0.1",
    allowed_initiator_urls: list[str] | None = None,
) -> MemberAgent:
    folder = data_dir / member
    list_path = folder / "list.csv"
    rows = load_list(list_path)
    private_key, public_key = load_or_create_keypair(folder / "key.json")
    commitment = build_commitment(
        agent_id=f"member_{member.lower()}",
        structure=DataStructure.BLACKLIST,
        entry_count=len(rows),
        data_hash=list_digest(rows),
        last_updated=datetime.fromtimestamp(list_path.stat().st_mtime, UTC),
    )
    allowed = allowed_initiator_urls or [DEFAULT_INITIATOR_URL]
    agent = MemberAgent(
        allowed_initiator_urls=allowed,
        name=f"Member {member}",
        description=f"Quiet Check consortium member {member}: abuse list (PSI receiver)",
        card_url=f"http://{host}:{port}",
        port=port,
        host=host,
        role="ap3_receiver",
        operation=PSIOperation(),
        commitment=commitment,
        private_key=private_key,
        public_key=public_key,
        receiver_config_provider=lambda: {"sanction_list": rows},
        # The initiator runs on localhost (and a private network on Render): ADR 0004. Safe only
        # because MemberAgent checks the allowlist before the SDK fetches the initiator's card.
        allow_private_initiator_urls=True,
    )
    logger.info(
        "Member %s: %d list entries, serving on %s:%d, initiators allowed: %s",
        member,
        len(rows),
        host,
        port,
        ", ".join(sorted(agent.allowed_initiators)),
    )
    return agent


async def serve(agent: PrivacyAgent) -> None:
    async with agent.serving():
        await agent.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Quiet Check consortium member.")
    parser.add_argument("member", choices=MEMBERS)
    parser.add_argument("--port", type=int, help="default: 8101 / 8102 / 8103 for A / B / C")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--data-dir", type=Path, help="default: $MEMBER_DATA_DIR or backend/var/members"
    )
    parser.add_argument(
        "--initiator-url",
        action="append",
        dest="initiator_urls",
        help=f"initiator allowed to query this member (repeatable; {DEFAULT_INITIATOR_URL})",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        agent = build_member(
            args.member,
            port=args.port or MEMBER_PORTS[args.member],
            data_dir=args.data_dir or member_data_dir(),
            host=args.host,
            allowed_initiator_urls=args.initiator_urls,
        )
    except ListFileError as exc:
        # Never start with an empty list: every check would silently come back clean.
        sys.exit(f"Member {args.member}: {exc}")
    asyncio.run(serve(agent))


if __name__ == "__main__":
    main()
