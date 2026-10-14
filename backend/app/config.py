"""Runtime settings, read from the environment (and the repo-root .env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
VAR_DIR = BACKEND_DIR / "var"  # git-ignored: member lists and keys, the initiator key

MEMBERS = ("A", "B", "C")
INITIATOR_PORT = 8100
MEMBER_PORTS = {"A": 8101, "B": 8102, "C": 8103}

load_dotenv(REPO_DIR / ".env")


def _default_member_urls() -> dict[str, str]:
    return {m: f"http://127.0.0.1:{port}" for m, port in MEMBER_PORTS.items()}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./quietcheck.db"
    initiator_port: int = INITIATOR_PORT
    initiator_key_path: Path = VAR_DIR / "initiator" / "key.json"
    member_urls: dict[str, str] = field(default_factory=_default_member_urls)
    # Seconds allowed for all 9 PSI sessions together before the check fails.
    check_timeout_s: float = 60.0
    start_initiator: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(database_url=os.environ.get("DATABASE_URL") or cls.database_url)


def member_data_dir() -> Path:
    """Root of the per-member folders (`<root>/<A|B|C>/list.csv`, `key.json`)."""
    return Path(os.environ.get("MEMBER_DATA_DIR") or VAR_DIR / "members")
