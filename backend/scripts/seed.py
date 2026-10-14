"""Seed the three member lists for the demo (D7). Idempotent.

    cd backend && uv run python scripts/seed.py      # or `make seed`
    uv run python scripts/seed.py --member B         # one member only (each Render member, D9)

Reads PERSONA_ANA_EMAIL, PERSONA_BEN_EMAIL and PERSONA_CY_EMAIL from the repo-root .env (the
three sandbox buyer accounts) and writes `<MEMBER_DATA_DIR>/<A|B|C>/list.csv`: about 300
synthetic entries each, plus Ben's email on Member B and Cy's email and device ID on Members A
and C (`app/personas.json`). Running it again with the same emails rewrites nothing. Members read
their list at startup, so restart any running member after a change. Prints counts only.
"""

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.config import MEMBERS, member_data_dir  # noqa: E402
from app.consortium.common import list_digest  # noqa: E402
from app.demo_data import (  # noqa: E402
    build_lists,
    build_member_list,
    emails_needed,
    unseeded_persona_hashes,
    write_member_lists,
)
from app.personas import persona_emails_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the demo member lists.")
    parser.add_argument("--member", choices=MEMBERS, help="write only this member's list")
    parser.add_argument(
        "--digests",
        action="store_true",
        help="write nothing; print MEMBER_DATA_HASHES (each list's commitment data_hash) for the "
        "backend's /health/deep. Needs all three emails",
    )
    args = parser.parse_args()
    if args.digests:
        try:
            lists = build_lists(persona_emails_from_env())
        except ValueError as exc:
            print(f"seed: {exc}", file=sys.stderr)
            return 1
        print("MEMBER_DATA_HASHES=" + ",".join(f"{m}:{list_digest(lists[m])}" for m in MEMBERS))
        return 0
    only = args.member
    members = [only] if only else list(MEMBERS)
    try:
        # One member needs only the emails seeded on its own list (hosted members, D9).
        emails = persona_emails_from_env(emails_needed(only) if only else None)
    except ValueError as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 1
    if only:
        lists = {only: build_member_list(only, emails)}
    else:
        lists = build_lists(emails)
        forbidden = unseeded_persona_hashes(emails)
        for member in MEMBERS:
            if forbidden[member] & set(lists[member]):
                print(
                    f"seed: a persona identifier landed on Member {member}'s list", file=sys.stderr
                )
                return 1
    data_dir = member_data_dir()
    changed = write_member_lists(data_dir, lists)
    for member in members:
        state = "written" if changed[member] else "unchanged"
        print(f"Member {member}: {len(lists[member])} entries, {state}")
    print(f"Lists in {data_dir}.")
    if any(changed.values()):
        print("Restart any running members (make dev) so they load the new lists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
