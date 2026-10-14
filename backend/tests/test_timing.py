"""Timing at demo size (~300 entries per member). Opt-in: `uv run pytest -m slow -s`."""

import time

import pytest
from fastapi.testclient import TestClient

from app.config import MEMBERS
from app.main import create_app
from tests import cases
from tests.conftest import make_settings
from tests.harness import MemberProcess, free_port, write_lists
from tests.test_risk_check import run_check

pytestmark = pytest.mark.slow


def test_check_takes_about_3s_at_300_entries(run_dir) -> None:
    data_dir = run_dir / "members-300"
    write_lists(data_dir, cases.seeded_hashes(), filler_per_member=295)  # ~300 entries each
    members = [MemberProcess(m, free_port(), data_dir, run_dir / "logs") for m in MEMBERS]
    try:
        for p in members:
            p.wait_ready()
        urls = {p.member: p.url for p in members}
        app = create_app(make_settings(run_dir, "timing", urls))
        with TestClient(app) as client:
            timings = []
            for name in ("ana", "ben", "cy"):
                start = time.perf_counter()
                response = run_check(client, cases.person(name))
                timings.append(time.perf_counter() - start)
                assert response.status_code == 200
        print(
            f"\n/risk/check at ~300 entries per member: {', '.join(f'{t:.2f}s' for t in timings)}"
        )
        assert max(timings) < 6.0
    finally:
        for p in members:
            p.stop()
