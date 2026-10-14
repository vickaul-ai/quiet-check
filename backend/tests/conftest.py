"""Shared fixtures. Integration tests run the three real member processes.

Every log line from the run lands in `backend/var/test-run/` (or $QC_TEST_RUN_DIR), together
with the member lists and `sensitive.txt`. `scripts/scan_logs.py` then fails if any list entry
or identifier appears in a log (contracts.md: no list contents in logs).
"""

import logging
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BACKEND_DIR, MEMBERS, Settings
from app.demo_data import persona_seeds
from app.main import create_app
from tests import cases
from tests.fake_model import FakeModel
from tests.fake_paypal import WEBHOOK_ID, FakePayPal
from tests.harness import MemberProcess, free_port, write_lists

RUN_DIR = Path(os.environ.get("QC_TEST_RUN_DIR") or BACKEND_DIR / "var" / "test-run")


@pytest.fixture(scope="session", autouse=True)
def run_dir() -> Path:
    shutil.rmtree(RUN_DIR, ignore_errors=True)
    (RUN_DIR / "logs").mkdir(parents=True)
    handler = logging.FileHandler(RUN_DIR / "logs" / "backend.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield RUN_DIR
    root.removeHandler(handler)
    handler.close()


@pytest.fixture(scope="session")
def consortium(run_dir: Path):
    """Members A, B, C on spare ports with small synthetic lists (fast PSI): the D2 cases plus
    the demo personas' seeds (with test emails).

    Members answer only allowlisted initiators, so the initiator ports of the test apps that
    use these members are reserved here and passed to each member as `--initiator-url`.
    """
    data_dir = run_dir / "members"
    seeded = cases.seeded_hashes()
    for member, hashes in persona_seeds(cases.PERSONA_EMAILS).items():
        seeded[member] = seeded.get(member, []) + hashes
    write_lists(data_dir, seeded, filler_per_member=30)
    (run_dir / "sensitive.txt").write_text("\n".join(cases.sensitive_values()) + "\n")
    initiator_ports = {"main": free_port(), "member-down": free_port(), "merchant": free_port()}
    initiator_urls = [f"http://127.0.0.1:{p}" for p in initiator_ports.values()]
    members = {
        m: MemberProcess(m, free_port(), data_dir, run_dir / "logs", initiator_urls)
        for m in MEMBERS
    }
    try:
        for proc in members.values():
            proc.wait_ready()
        yield {
            "data_dir": data_dir,
            "members": members,
            "initiator_ports": initiator_ports,
            "initiator_urls": initiator_urls,
        }
    finally:
        for proc in members.values():
            proc.stop()


def test_database_url(run_dir: Path, name: str) -> str:
    """SQLite per test app, or with QC_TEST_POSTGRES_URL (e.g. postgresql://localhost/postgres)
    a fresh Postgres database per test app, as on Render (D9)."""
    base = os.environ.get("QC_TEST_POSTGRES_URL")
    if not base:
        return f"sqlite:///{run_dir / f'{name}.db'}"
    import hashlib

    import psycopg

    db = "qc_test_" + hashlib.sha256(name.encode()).hexdigest()[:16]
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {db}")
        conn.execute(f"CREATE DATABASE {db}")
    return base.rsplit("/", 1)[0] + "/" + db


def make_settings(
    run_dir: Path, name: str, member_urls: dict[str, str], initiator_port: int, **kw
) -> Settings:
    return Settings(
        database_url=test_database_url(run_dir, name),
        initiator_port=initiator_port,
        initiator_key_path=run_dir / "initiator" / "key.json",
        member_urls=member_urls,
        **kw,
    )


@pytest.fixture(scope="session")
def client(run_dir: Path, consortium):
    urls = {m: p.url for m, p in consortium["members"].items()}
    port = consortium["initiator_ports"]["main"]
    app = create_app(make_settings(run_dir, "main", urls, port))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake() -> FakePayPal:
    """A fresh in-memory PayPal sandbox (tests/fake_paypal.py)."""
    return FakePayPal()


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


def paypal_settings(fake: FakePayPal, model: FakeModel) -> dict:
    return {
        "paypal_client_id": "fake-id",
        "paypal_client_secret": "fake-secret",
        "paypal_webhook_id": WEBHOOK_ID,
        "paypal_transport": fake.transport,
        "rationale_model": model,
        "rationale_timeout_s": 1.0,
    }


@pytest.fixture
def api(run_dir: Path, fake: FakePayPal, model: FakeModel, request):
    """An app wired to the fake PayPal and model, with no AP3 initiator. /merchant/process
    works here on orders that have a stored check (tests/stored_checks.py)."""
    settings = make_settings(
        run_dir,
        f"payments-{request.node.name}",
        {},
        free_port(),
        start_initiator=False,
        **paypal_settings(fake, model),
    )
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture(scope="session")
def merchant_fakes() -> tuple[FakePayPal, FakeModel]:
    return FakePayPal(), FakeModel()


@pytest.fixture(scope="session")
def merchant_app(run_dir: Path, consortium, merchant_fakes):
    """The whole backend: real members and initiator, fake PayPal and model (session-wide)."""
    fake, model = merchant_fakes
    urls = {m: p.url for m, p in consortium["members"].items()}
    settings = make_settings(
        run_dir,
        "merchant",
        urls,
        consortium["initiator_ports"]["merchant"],
        **paypal_settings(fake, model),
    )
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def merchant(merchant_app, merchant_fakes):
    """(client, fake PayPal, fake model), with the model reset for each test."""
    fake, model = merchant_fakes
    model.mode, model.calls[:] = "ok", []
    model.reply = "Fake rationale: the policy decided this from the hit summary."
    return merchant_app, fake, model
