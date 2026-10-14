"""Consortium member: one AP3 receiver (PSI) holding one private list.

Run once per member:

    uv run python -m app.consortium.member A    # :8101; B on :8102, C on :8103

Reads `<MEMBER_DATA_DIR>/<member>/list.csv` and `key.json` (default `backend/var/members/`).
The key is created on first start and kept, so the member's identity survives restarts.
The list is read once at startup; restart the member after reseeding.
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from ap3.a2a import PrivacyAgent
from ap3.types import DataStructure
from ap3_functions import PSIOperation

from app.config import MEMBER_PORTS, MEMBERS, member_data_dir
from app.consortium.common import (
    ListFileError,
    build_commitment,
    list_digest,
    load_list,
    load_or_create_keypair,
)

logger = logging.getLogger("quietcheck.member")


def build_member(member: str, port: int, data_dir: Path, host: str = "127.0.0.1") -> PrivacyAgent:
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
    agent = PrivacyAgent(
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
        # The initiator runs on localhost (and a private network on Render): ADR 0004.
        allow_private_initiator_urls=True,
    )
    logger.info("Member %s: %d list entries, serving on %s:%d", member, len(rows), host, port)
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        agent = build_member(
            args.member,
            port=args.port or MEMBER_PORTS[args.member],
            data_dir=args.data_dir or member_data_dir(),
            host=args.host,
        )
    except ListFileError as exc:
        # Never start with an empty list: every check would silently come back clean.
        sys.exit(f"Member {args.member}: {exc}")
    asyncio.run(serve(agent))


if __name__ == "__main__":
    main()
