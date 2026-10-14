"""Keys, list files and commitments shared by members and the initiator."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ap3.signing.primitives import generate_keypair
from ap3.types import CommitmentMetadata, DataFormat, DataFreshness, DataStructure, Industry

_HASH_LINE = re.compile(r"^[0-9a-f]{64}$")


def load_or_create_keypair(path: Path) -> tuple[bytes, bytes]:
    """Ed25519 (private, public) key bytes, stored as hex in JSON. Created on first use."""
    if path.exists():
        data = json.loads(path.read_text())
        return bytes.fromhex(data["private_key"]), bytes.fromhex(data["public_key"])
    private_key, public_key = generate_keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"private_key": private_key.hex(), "public_key": public_key.hex()})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return private_key, public_key


class ListFileError(Exception):
    pass


def load_list(path: Path) -> list[str]:
    """A member list: one lowercase hex sha256 per line, no header. Blank lines are ignored.

    Errors name the line number only, never the line's contents.
    """
    if not path.exists():
        raise ListFileError(f"list file not found: {path} (D7's seed script writes it)")
    rows: list[str] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if not _HASH_LINE.match(line):
            raise ListFileError(f"{path}: line {number} is not a lowercase sha256 hex hash")
        rows.append(line)
    if not rows:
        raise ListFileError(f"list file is empty: {path}")
    return rows


def list_digest(rows: list[str]) -> str:
    """sha256 over the sorted list, published as the commitment's `data_hash`."""
    return hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()


def build_commitment(
    agent_id: str,
    structure: DataStructure,
    entry_count: int,
    data_hash: str | None = None,
    last_updated: datetime | None = None,
) -> CommitmentMetadata:
    """An unsigned commitment; PrivacyAgent signs it with the agent's key at construction."""
    return CommitmentMetadata(
        agent_id=agent_id,
        commitment_id=f"{agent_id}_{(data_hash or 'v1')[:12]}",
        data_structure=structure,
        data_format=DataFormat.STRUCTURED,
        entry_count=entry_count,
        field_count=1,
        estimated_size_mb=round(entry_count * 65 / 1_000_000, 4),
        last_updated=(last_updated or datetime.now(UTC)).isoformat(),
        data_freshness=DataFreshness.DAILY,
        industry=Industry.FINANCE,
        data_hash=data_hash,
    )
