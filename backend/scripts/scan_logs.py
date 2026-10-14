"""Fail if any member list entry or test identifier appears in a log (contracts.md).

    uv run python scripts/scan_logs.py [RUN_DIR]     # default: var/test-run

Needles: every line of every `RUN_DIR/members/*/list.csv` plus `RUN_DIR/sensitive.txt` (raw,
normalised and hashed identifiers from the test cases). Haystack: every `*.log` under RUN_DIR.
Prints only file names and counts, never a matched value.
"""

import sys
from pathlib import Path


def main() -> int:
    run_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "var/test-run")
    needles: set[str] = set()
    lists = sorted(run_dir.glob("members/*/list.csv"))
    for path in [*lists, run_dir / "sensitive.txt"]:
        if path.exists():
            needles.update(line.strip() for line in path.read_text().splitlines() if line.strip())
    logs = sorted(run_dir.rglob("*.log"))
    if len(lists) < 3 or not needles or not logs:
        print(f"scan_logs: nothing to scan in {run_dir} (run pytest first)", file=sys.stderr)
        return 2
    leaks = 0
    for log in logs:
        text = log.read_text(errors="replace").lower()
        found = sum(1 for needle in needles if needle.lower() in text)
        if found:
            leaks += found
            print(
                f"scan_logs: {log}: {found} list entr(y/ies) or identifier(s) found",
                file=sys.stderr,
            )
    size = sum(log.stat().st_size for log in logs)
    if leaks:
        return 1
    print(f"scan_logs: OK, {len(needles)} values absent from {len(logs)} logs ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
