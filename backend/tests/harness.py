"""Test harness: synthetic member lists and real member processes."""

import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from app.config import BACKEND_DIR, MEMBERS
from app.identifiers import FIELDS, hash_normalised


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def filler(n: int) -> list[str]:
    """Synthetic entries spread over the three fields; they match nothing real."""
    return [hash_normalised(FIELDS[i % 3], f"filler-{secrets.token_hex(8)}") for i in range(n)]


def write_lists(data_dir: Path, seeded: dict[str, list[str]], filler_per_member: int) -> None:
    for member in MEMBERS:
        folder = data_dir / member
        folder.mkdir(parents=True, exist_ok=True)
        rows = filler(filler_per_member) + seeded.get(member, [])
        (folder / "list.csv").write_text("\n".join(rows) + "\n")


class MemberProcess:
    def __init__(
        self, member: str, port: int, data_dir: Path, log_dir: Path, initiator_urls: list[str]
    ):
        self.member, self.port = member, port
        self.url = f"http://127.0.0.1:{port}"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log = open(log_dir / f"member-{member}-{port}.log", "ab")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.consortium.member",
                member,
                "--port",
                str(port),
                "--data-dir",
                str(data_dir),
                *[arg for url in initiator_urls for arg in ("--initiator-url", url)],
            ],
            cwd=BACKEND_DIR,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 30.0) -> "MemberProcess":
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"member {self.member} exited with {self.proc.returncode}")
            try:
                if (
                    httpx.get(f"{self.url}/.well-known/agent-card.json", timeout=1).status_code
                    == 200
                ):
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"member {self.member} not ready on {self.url}")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()
