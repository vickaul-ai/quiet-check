"""Runtime settings, read from the environment (and the repo-root .env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
VAR_DIR = BACKEND_DIR / "var"  # git-ignored: member lists and keys, the initiator key

MEMBERS = ("A", "B", "C")
INITIATOR_PORT = 8100
MEMBER_PORTS = {"A": 8101, "B": 8102, "C": 8103}
PAYPAL_SANDBOX_URL = "https://api-m.sandbox.paypal.com"

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
    # PayPal REST app (sandbox only). Tests pass an httpx transport that fakes PayPal.
    paypal_base_url: str = PAYPAL_SANDBOX_URL
    paypal_client_id: str = ""
    paypal_client_secret: str = field(default="", repr=False)
    paypal_webhook_id: str = ""
    paypal_transport: Any = None
    # Risk rationale model (contracts.md, "Environment variables"). Tests pass `rationale_model`,
    # a LangChain chat model (or anything with `ainvoke`), instead of building one.
    llm_provider: str = "anthropic"
    llm_model_fast: str = "claude-haiku-5-5"
    rationale_timeout_s: float = 10.0
    rationale_model: Any = None
    # Run /merchant/process when a CHECKOUT.ORDER.APPROVED webhook moves an order to approved.
    # Off only for scripts/approve_orders.py, which saves approved orders for later live runs.
    process_on_approval: bool = True

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        values = {
            "database_url": os.environ.get("DATABASE_URL") or cls.database_url,
            "paypal_client_id": os.environ.get("PAYPAL_CLIENT_ID", ""),
            "paypal_client_secret": os.environ.get("PAYPAL_CLIENT_SECRET", ""),
            "paypal_webhook_id": os.environ.get("PAYPAL_WEBHOOK_ID", ""),
            "llm_provider": os.environ.get("LLM_PROVIDER") or cls.llm_provider,
            "llm_model_fast": os.environ.get("LLM_MODEL_FAST") or cls.llm_model_fast,
        }
        return cls(**(values | overrides))


def member_data_dir() -> Path:
    """Root of the per-member folders (`<root>/<A|B|C>/list.csv`, `key.json`)."""
    return Path(os.environ.get("MEMBER_DATA_DIR") or VAR_DIR / "members")
