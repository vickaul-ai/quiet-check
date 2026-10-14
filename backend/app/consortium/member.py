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
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from ap3.a2a import PrivacyAgent
from ap3.a2a.card import normalize_url
from ap3.a2a.wire import ProtocolEnvelope
from ap3.types import DataStructure, PrivacyError
from ap3_functions import PSIOperation

from app.config import (
    INITIATOR_PORT,
    MEMBER_PORTS,
    MEMBERS,
    default_bind_host,
    member_data_dir,
    own_private_url,
    private_url,
)
from app.consortium.common import (
    ListFileError,
    build_commitment,
    list_digest,
    load_keypair,
    load_list,
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

    def __init__(
        self,
        *,
        allowed_initiator_urls: list[str],
        allowed_initiator_pattern: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.allowed_initiators = frozenset(normalize_url(u) for u in allowed_initiator_urls)
        # Hosted (D9): Render gives the backend's internal hostname a suffix we can't know in
        # the Blueprint without a reference cycle, so a full-match pattern on the normalised URL
        # (e.g. http://quietcheck-backend(-[a-z0-9]+)?:8100) can stand in for it.
        self.allowed_pattern = (
            re.compile(allowed_initiator_pattern) if allowed_initiator_pattern else None
        )

    def initiator_allowed(self, envelope: ProtocolEnvelope) -> bool:
        intent = envelope.privacy_intent
        if intent is None:
            return True  # no URL to fetch; the SDK refuses a session opened without an intent
        participants = intent.get("participants") if isinstance(intent, dict) else None
        if not isinstance(participants, list) or not participants:
            return False
        initiator = participants[0]
        if not isinstance(initiator, str):
            return False
        url = normalize_url(initiator)
        return url in self.allowed_initiators or bool(
            self.allowed_pattern and self.allowed_pattern.fullmatch(url)
        )

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
    card_url: str | None = None,
    private_key: str | None = None,
    allowed_initiator_pattern: str | None = None,
) -> MemberAgent:
    folder = data_dir / member
    list_path = folder / "list.csv"
    rows = load_list(list_path)
    private_key, public_key = load_keypair(folder / "key.json", private_key)
    commitment = build_commitment(
        agent_id=f"member_{member.lower()}",
        structure=DataStructure.BLACKLIST,
        entry_count=len(rows),
        data_hash=list_digest(rows),
        last_updated=datetime.fromtimestamp(list_path.stat().st_mtime, UTC),
    )
    # With only a pattern, the localhost default isn't added.
    allowed = allowed_initiator_urls or (
        [] if allowed_initiator_pattern else [DEFAULT_INITIATOR_URL]
    )
    agent = MemberAgent(
        allowed_initiator_urls=allowed,
        allowed_initiator_pattern=allowed_initiator_pattern,
        name=f"Member {member}",
        description=f"Quiet Check consortium member {member}: abuse list (PSI receiver)",
        # Must equal the URL the initiator uses for this member (the SDK checks it).
        card_url=card_url or f"http://{host}:{port}",
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
        "Member %s: %d list entries, serving on %s:%d as %s, initiators allowed: %s",
        member,
        len(rows),
        host,
        port,
        card_url or f"http://{host}:{port}",
        ", ".join(sorted(agent.allowed_initiators))
        + (f", or matching {allowed_initiator_pattern}" if allowed_initiator_pattern else ""),
    )
    return agent


async def serve(agent: PrivacyAgent) -> None:
    async with agent.serving():
        await agent.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Quiet Check consortium member.")
    parser.add_argument("member", choices=MEMBERS)
    parser.add_argument("--port", type=int, help="default: 8101 / 8102 / 8103 for A / B / C")
    parser.add_argument("--host", help="listen address (default 127.0.0.1; 0.0.0.0 on Render)")
    parser.add_argument(
        "--card-url",
        help="URL the initiator reaches this member at (default $MEMBER_CARD_URL; on Render "
        "http://$RENDER_SERVICE_NAME:<port>; else http://<host>:<port>). It must equal the "
        "backend's MEMBER_<M>_URL",
    )
    parser.add_argument(
        "--data-dir", type=Path, help="default: $MEMBER_DATA_DIR or backend/var/members"
    )
    parser.add_argument(
        "--initiator-url",
        action="append",
        dest="initiator_urls",
        help="initiator allowed to query this member (repeatable; default $INITIATOR_URL, "
        f"or http://$INITIATOR_HOST:{INITIATOR_PORT}, or {DEFAULT_INITIATOR_URL})",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = args.port or MEMBER_PORTS[args.member]
    # Hosted (D9): the Blueprint gives each member the backend's internal hostname.
    initiator_host = os.environ.get("INITIATOR_HOST")
    initiator_url = os.environ.get("INITIATOR_URL") or (
        initiator_host and private_url(initiator_host, INITIATOR_PORT)
    )
    try:
        agent = build_member(
            args.member,
            port=port,
            data_dir=args.data_dir or member_data_dir(),
            host=args.host or default_bind_host(),
            allowed_initiator_urls=args.initiator_urls
            or ([initiator_url] if initiator_url else None),
            card_url=args.card_url or os.environ.get("MEMBER_CARD_URL") or own_private_url(port),
            # Hosted (D9): the key comes from the env, since the disk is wiped on deploy.
            private_key=os.environ.get("MEMBER_PRIVATE_KEY"),
            allowed_initiator_pattern=os.environ.get("INITIATOR_URL_PATTERN") or None,
        )
    except (ListFileError, ValueError, re.error) as exc:
        # Never start with a missing or empty list (every check would come back clean) or a bad key.
        sys.exit(f"Member {args.member}: {exc}")
    asyncio.run(serve(agent))


if __name__ == "__main__":
    main()
