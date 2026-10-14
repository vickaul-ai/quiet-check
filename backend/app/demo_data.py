"""Synthetic member lists for the demo (D7). No real people.

Each member gets about 300 entries: synthetic people's emails (reserved `example.*` domains),
shipping addresses and device IDs, generated from a fixed per-member random seed, plus the
persona seeds from `personas.json`. The same persona emails always produce byte-identical files,
so seeding is idempotent and each member's commitment `data_hash` stays stable.
"""

import random
from pathlib import Path

from app.config import MEMBERS
from app.identifiers import identifier_hash
from app.personas import load_personas

ENTRIES_PER_MEMBER = 300  # including persona seeds (ADR 0004: PSI cost grows with list size)

FIRST = (
    "Avery Blake Casey Dana Elliot Finley Gray Harper Indigo Jules Kai Logan Morgan Noel Oakley "
    "Parker Quinn Reese Sage Taylor Umber Vale Wren Yael Zion"
).split()
LAST = (
    "Ashdown Brightwell Calloway Dunmore Everly Fairbanks Gallow Hartwell Ingram Jessop Kestrel "
    "Lindqvist Marlow Northcott Osgood Pembury Quill Rook Sterling Thorne Upton Vickery Waverly "
    "Yarrow Zeller"
).split()
STREETS = (
    "Alder Birch Cobalt Driftwood Ember Fernhill Glade Heron Ivy Juniper Kettle Lantern Mossy "
    "Nettle Orchard Pebble Quarry Rushmore Saltmarsh Thistle"
).split()
SUFFIXES = ("St", "Ave", "Rd", "Ln", "Ct", "Way", "Pl", "Blvd")
# Made-up towns; the states and ZIP prefixes only shape the data.
TOWNS = (
    ("Fernvale", "CA", "940"),
    ("Millbrook", "NY", "125"),
    ("Cedar Hollow", "TX", "787"),
    ("Lakemont", "WA", "981"),
    ("Brightwater", "IL", "606"),
    ("Stonebridge", "FL", "331"),
    ("Ashford Bay", "OR", "972"),
    ("Pinecrest", "CO", "802"),
)
DOMAINS = ("example.com", "example.net", "example.org")


def _person(rng: random.Random) -> dict:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    town, state, zip_prefix = rng.choice(TOWNS)
    street = f"{rng.randint(1, 9999)} {rng.choice(STREETS)} {rng.choice(SUFFIXES)}"
    return {
        "email": f"{first}.{last}{rng.randint(1, 9999)}@{rng.choice(DOMAINS)}".lower(),
        "shipping_address": {
            "address_line_1": street,
            "address_line_2": rng.choice(("", "", "", f"Apt {rng.randint(1, 40)}")),
            "admin_area_2": town,
            "admin_area_1": state,
            "postal_code": f"{zip_prefix}{rng.randint(0, 99):02d}",
            "country_code": "US",
        },
        "device_id": f"dev-{rng.getrandbits(48):012x}",
    }


def persona_seeds(emails: dict[str, str], members=MEMBERS) -> dict[str, list[str]]:
    """{member: [hash, ...]} for the personas, per `seeded_on` in personas.json. Needs only the
    emails of the personas whose email is seeded on these members (`emails_needed`)."""
    seeds: dict[str, list[str]] = {m: [] for m in members}
    for persona in load_personas():
        for member, fields in persona["seeded_on"].items():
            if member not in seeds:
                continue
            for field in fields:
                value = persona["device_id"] if field == "device_id" else emails[persona["key"]]
                seeds[member].append(identifier_hash(field, value))
    return seeds


def emails_needed(member: str) -> list[str]:
    """Keys of the personas whose email is on this member's list (D9: a hosted member gets
    only those emails)."""
    return [p["key"] for p in load_personas() if "email" in p["seeded_on"].get(member, [])]


def synthetic_entries(member: str, count: int) -> list[str]:
    """`count` distinct hashes of synthetic identifiers, the same on every run."""
    rng = random.Random(f"quiet-check-demo-{member}")
    entries: set[str] = set()
    while len(entries) < count:
        person = _person(rng)
        # Mostly one field per synthetic person; sometimes two, as a real abuse list might.
        fields = rng.sample(("email", "shipping_address", "device_id"), rng.choice((1, 1, 1, 2)))
        for field in fields:
            if len(entries) < count:
                entries.add(identifier_hash(field, person[field]))
    return sorted(entries)


def build_member_list(member: str, emails: dict[str, str]) -> list[str]:
    """One member's list: synthetic entries plus its persona seeds, ENTRIES_PER_MEMBER in all."""
    seeds = persona_seeds(emails, members=(member,))[member]
    filler = synthetic_entries(member, ENTRIES_PER_MEMBER - len(seeds))
    return sorted(set(filler) | set(seeds))


def build_lists(emails: dict[str, str]) -> dict[str, list[str]]:
    """{member: sorted hashes}: synthetic entries plus persona seeds, ENTRIES_PER_MEMBER each."""
    return {member: build_member_list(member, emails) for member in MEMBERS}


def unseeded_persona_hashes(emails: dict[str, str]) -> dict[str, set[str]]:
    """{member: hashes of persona identifiers that must NOT be on that member's list}. Only
    emails and device IDs: persona addresses come from the sandbox accounts."""
    out: dict[str, set[str]] = {m: set() for m in MEMBERS}
    for persona in load_personas():
        values = {"email": emails[persona["key"]], "device_id": persona["device_id"]}
        for member in MEMBERS:
            seeded = set(persona["seeded_on"].get(member, []))
            out[member] |= {identifier_hash(f, v) for f, v in values.items() if f not in seeded}
    return out


def write_member_lists(data_dir: Path, lists: dict[str, list[str]]) -> dict[str, bool]:
    """Write `<data_dir>/<M>/list.csv` (one hash per line). Returns {member: changed}."""
    changed = {}
    for member, entries in lists.items():
        path = data_dir / member / "list.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(entries) + "\n"
        changed[member] = not path.exists() or path.read_text() != content
        if changed[member]:
            path.write_text(content)
    return changed
