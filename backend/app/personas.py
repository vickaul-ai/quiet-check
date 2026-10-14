"""The three demo personas (docs/brief.md, "Personas"; D7).

`personas.json` holds what is safe to publish: device IDs (synthetic), where each persona is
seeded and the expected outcome. Each persona's email is that of a sandbox buyer account, so it
comes from `.env` (`PERSONA_ANA_EMAIL`, `PERSONA_BEN_EMAIL`, `PERSONA_CY_EMAIL`), never the repo.
"""

import json
import os
from functools import cache
from pathlib import Path

PERSONAS_PATH = Path(__file__).with_name("personas.json")


@cache
def load_personas() -> list[dict]:
    return json.loads(PERSONAS_PATH.read_text())


def persona_emails_from_env(keys: list[str] | None = None) -> dict[str, str]:
    """{persona key: email} for the given personas (default all). Raises ValueError naming
    any unset or repeated variable."""
    personas = [p for p in load_personas() if keys is None or p["key"] in keys]
    emails = {p["key"]: os.environ.get(p["email_env"], "").strip() for p in personas}
    missing = [p["email_env"] for p in personas if not emails[p["key"]]]
    if missing:
        raise ValueError(f"set {', '.join(missing)} in .env (the sandbox buyer emails)")
    if len({e.lower() for e in emails.values()}) != len(emails):
        raise ValueError("each persona needs its own sandbox buyer email")
    return emails
