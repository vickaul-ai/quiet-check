"""Unit tests: the decision policy, the template rationale and the D7 seed data."""

import subprocess
import sys

import pytest

from app.config import BACKEND_DIR, MEMBERS
from app.demo_data import (
    ENTRIES_PER_MEMBER,
    build_lists,
    persona_seeds,
    unseeded_persona_hashes,
    write_member_lists,
)
from app.identifiers import FIELDS, identifier_hash
from app.personas import load_personas
from app.policy import decide
from app.rationale import hit_summary, template_rationale
from tests import cases


def hits(*matched: tuple[str, str]) -> list[dict]:
    return [
        {"member": m, "field": f, "is_match": (m, f) in matched} for m in MEMBERS for f in FIELDS
    ]


@pytest.mark.parametrize(
    ("matched", "expected"),
    [
        ((), "clean"),
        ((("B", "email"),), "hold"),
        ((("A", "device_id"),), "hold"),
        ((("A", "shipping_address"), ("C", "shipping_address")), "block"),
        ((("A", "email"), ("B", "device_id")), "block"),  # email and device at any members
        ((("B", "email"), ("B", "shipping_address"), ("B", "device_id")), "block"),
    ],
)
def test_policy(matched, expected) -> None:
    assert decide(hits(*matched)) == expected


def test_policy_email_and_device_clause_stands_alone() -> None:
    """If hit counting ever collapsed per field, email + device would still block."""
    collapsed = [
        {"member": "*", "field": "email", "is_match": True},
        {"member": "*", "field": "device_id", "is_match": True},
    ]
    assert decide(collapsed) == "block"
    assert decide(collapsed[:1]) == "hold"


@pytest.mark.parametrize(
    ("decision", "matched", "text"),
    [
        (
            "hold",
            (("B", "email"),),
            "Automatic explanation unavailable. Policy decision: hold. 1 hit: Member B, email. "
            "Address and device were clear at all members.",
        ),
        (
            "clean",
            (),
            "Automatic explanation unavailable. Policy decision: clean. No hits. "
            "Email, address and device were clear at all members.",
        ),
        (
            "block",
            (("A", "email"), ("A", "device_id"), ("C", "email"), ("C", "device_id")),
            "Automatic explanation unavailable. Policy decision: block. 4 hits: Member A, email; "
            "Member A, device; Member C, email; Member C, device. "
            "Address was clear at all members.",
        ),
    ],
)
def test_template_rationale(decision, matched, text) -> None:
    assert template_rationale(hit_summary(decision, hits(*matched))) == text


EMAILS = cases.PERSONA_EMAILS


def test_lists_have_300_entries_and_the_persona_seeds() -> None:
    lists = build_lists(EMAILS)
    by_key = {p["key"]: p for p in load_personas()}
    for member in MEMBERS:
        assert len(lists[member]) == ENTRIES_PER_MEMBER
        assert lists[member] == sorted(set(lists[member]))
    ben, cy = EMAILS["ben"], EMAILS["cy"]
    cy_device = by_key["cy"]["device_id"]
    assert identifier_hash("email", ben) in lists["B"]
    for member in ("A", "C"):
        assert identifier_hash("email", cy) in lists[member]
        assert identifier_hash("device_id", cy_device) in lists[member]
    assert persona_seeds(EMAILS) == {
        "A": [identifier_hash("email", cy), identifier_hash("device_id", cy_device)],
        "B": [identifier_hash("email", ben)],
        "C": [identifier_hash("email", cy), identifier_hash("device_id", cy_device)],
    }
    for member, forbidden in unseeded_persona_hashes(EMAILS).items():
        assert not forbidden & set(lists[member])


def test_lists_differ_by_member_and_are_stable() -> None:
    first, second = build_lists(EMAILS), build_lists(EMAILS)
    assert first == second
    assert len(set(first["A"]) & set(first["B"])) < 5


def test_write_is_idempotent(tmp_path) -> None:
    lists = build_lists(EMAILS)
    assert write_member_lists(tmp_path, lists) == {m: True for m in MEMBERS}
    before = {m: (tmp_path / m / "list.csv").read_bytes() for m in MEMBERS}
    assert write_member_lists(tmp_path, lists) == {m: False for m in MEMBERS}
    assert {m: (tmp_path / m / "list.csv").read_bytes() for m in MEMBERS} == before


def test_seed_script(tmp_path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "MEMBER_DATA_DIR": str(tmp_path),
        "PERSONA_ANA_EMAIL": EMAILS["ana"],
        "PERSONA_BEN_EMAIL": EMAILS["ben"],
        "PERSONA_CY_EMAIL": EMAILS["cy"],
    }
    script = [sys.executable, "scripts/seed.py"]
    first = subprocess.run(script, cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "Member A: 300 entries, written" in first.stdout
    second = subprocess.run(script, cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert "Member B: 300 entries, unchanged" in second.stdout
    for email in EMAILS.values():
        assert email not in first.stdout + second.stdout

    same = {**env, "PERSONA_CY_EMAIL": EMAILS["ben"]}
    dup = subprocess.run(script, cwd=BACKEND_DIR, env=same, capture_output=True, text=True)
    assert dup.returncode == 1 and "own sandbox buyer email" in dup.stderr
