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


def private_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def own_private_url(port: int) -> str | None:
    """This service's URL on Render's private network, or None off Render. Render's default
    `RENDER_SERVICE_NAME` is the internal hostname other services reach us at (D9)."""
    name = os.environ.get("RENDER_SERVICE_NAME")
    return private_url(name, port) if os.environ.get("RENDER") and name else None


def _risk_workflow_task() -> str:
    """`RISK_WORKFLOW_TASK`, else `<RISK_WORKFLOW_SLUG>/process_order` (the Blueprint gives the
    workflow's slug), else "" (run the risk agent in-process)."""
    slug = os.environ.get("RISK_WORKFLOW_SLUG")
    return os.environ.get("RISK_WORKFLOW_TASK") or (f"{slug}/process_order" if slug else "")


def default_bind_host() -> str:
    return "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"


def _member_urls_from_env() -> dict[str, str]:
    """Each member's URL: `MEMBER_A_URL`, else `MEMBER_A_HOST` (hosted: the member's internal
    hostname, from the Blueprint) on the member's port, else localhost."""
    urls = {}
    for member, port in MEMBER_PORTS.items():
        host = os.environ.get(f"MEMBER_{member}_HOST")
        url = os.environ.get(f"MEMBER_{member}_URL") or (host and private_url(host, port))
        urls[member] = (url or private_url("127.0.0.1", port)).rstrip("/")
    return urls


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./quietcheck.db"
    initiator_port: int = INITIATOR_PORT
    # Where the initiator listens, and the URL members reach it at (its AgentCard URL, which
    # members check against their allowlist). Hosted: 0.0.0.0 and http://<internal host>:8100.
    initiator_host: str = "127.0.0.1"
    initiator_url: str = ""  # default http://127.0.0.1:<initiator_port>
    initiator_key_path: Path = VAR_DIR / "initiator" / "key.json"
    # Hosted (D9): the key comes from INITIATOR_PRIVATE_KEY, since the disk is wiped on deploy.
    initiator_private_key: str = field(default="", repr=False)
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
    # Hosted (D9): every route except /health, /webhooks/paypal and the order status endpoint
    # needs `Authorization: Bearer <api_token>` (the web service and the risk workflow send it).
    # Empty: no auth, for localhost and tests.
    api_token: str = field(default="", repr=False)
    # Hosted (D9): start the risk agent as a Render Workflows task run (app/workflow.py), e.g.
    # "quietcheck-risk/process_order" (RISK_WORKFLOW_TASK or RISK_WORKFLOW_SLUG). Without it or
    # RENDER_API_KEY, the risk agent runs in-process.
    render_api_key: str = field(default="", repr=False)
    risk_workflow_task: str = ""
    render_api_url: str = "https://api.render.com/v1"
    render_transport: Any = None

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        initiator_port = int(os.environ.get("INITIATOR_PORT") or INITIATOR_PORT)
        values = {
            "database_url": os.environ.get("DATABASE_URL") or cls.database_url,
            "paypal_client_id": os.environ.get("PAYPAL_CLIENT_ID", ""),
            "paypal_client_secret": os.environ.get("PAYPAL_CLIENT_SECRET", ""),
            "paypal_webhook_id": os.environ.get("PAYPAL_WEBHOOK_ID", ""),
            "llm_provider": os.environ.get("LLM_PROVIDER") or cls.llm_provider,
            "llm_model_fast": os.environ.get("LLM_MODEL_FAST") or cls.llm_model_fast,
            "member_urls": _member_urls_from_env(),
            "initiator_port": initiator_port,
            "initiator_host": os.environ.get("INITIATOR_BIND") or default_bind_host(),
            "initiator_url": (
                os.environ.get("INITIATOR_URL") or own_private_url(initiator_port) or ""
            ).rstrip("/"),
            "initiator_private_key": os.environ.get("INITIATOR_PRIVATE_KEY", ""),
            "api_token": os.environ.get("BACKEND_API_TOKEN", ""),
            "render_api_key": os.environ.get("RENDER_API_KEY", ""),
            "risk_workflow_task": _risk_workflow_task(),
        }
        return cls(**(values | overrides))


def member_data_dir() -> Path:
    """Root of the per-member folders (`<root>/<A|B|C>/list.csv`, `key.json`)."""
    return Path(os.environ.get("MEMBER_DATA_DIR") or VAR_DIR / "members")
