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
from app.main import create_app
from tests import cases
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
    """Members A, B, C on spare ports with small synthetic lists (fast PSI)."""
    data_dir = run_dir / "members"
    write_lists(data_dir, cases.seeded_hashes(), filler_per_member=30)
    (run_dir / "sensitive.txt").write_text("\n".join(cases.sensitive_values()) + "\n")
    members = {m: MemberProcess(m, free_port(), data_dir, run_dir / "logs") for m in MEMBERS}
    try:
        for proc in members.values():
            proc.wait_ready()
        yield {"data_dir": data_dir, "members": members}
    finally:
        for proc in members.values():
            proc.stop()


def make_settings(run_dir: Path, name: str, member_urls: dict[str, str], **kw) -> Settings:
    return Settings(
        database_url=f"sqlite:///{run_dir / f'{name}.db'}",
        initiator_port=free_port(),
        initiator_key_path=run_dir / "initiator" / "key.json",
        member_urls=member_urls,
        **kw,
    )


@pytest.fixture(scope="session")
def client(run_dir: Path, consortium):
    urls = {m: p.url for m, p in consortium["members"].items()}
    app = create_app(make_settings(run_dir, "main", urls))
    with TestClient(app) as c:
        yield c
