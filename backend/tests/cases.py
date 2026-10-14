"""Synthetic people for the D2 risk-check cases. No real people; D7 owns the demo personas."""

from app.identifiers import identifier_hash, normalise


def address(line_1: str, line_2: str = "", postal: str = "62701", country: str = "US") -> dict:
    return {
        "address_line_1": line_1,
        "address_line_2": line_2,
        "admin_area_2": "Springfield",
        "admin_area_1": "IL",
        "postal_code": postal,
        "country_code": country,
    }


PEOPLE = {
    "ana": {
        "email": "ana@example.com",
        "shipping_address": address("1 Elm St"),
        "device_id": "dev-ana",
    },
    "ben": {
        "email": "ben.buyer@example.com",
        "shipping_address": address("2 Oak Ave"),
        "device_id": "dev-ben",
    },
    "cy": {
        "email": "cy@example.com",
        "shipping_address": address("3 Pine Rd"),
        "device_id": "dev-cy",
    },
    "dee": {
        "email": "dee@example.com",
        "shipping_address": address("12 Harbour St.", "Apt 4"),
        "device_id": "dev-dee",
    },
    "eve": {
        "email": "eve@example.com",
        "shipping_address": address("5 Birch Ln"),
        "device_id": "dev-eve",
    },
    "fay": {
        "email": "fay@example.com",
        "shipping_address": address("6 Cedar Ct"),
        "device_id": "dev-fay",
    },
    "gus": {
        "email": "gus@example.com",
        "shipping_address": address("7 Ash Way"),
        "device_id": "dev-gus",
    },
    "hal": {
        "email": "hal@example.com",
        "shipping_address": address("8 Fir Pl"),
        "device_id": "dev-hal",
    },
}

# Which (person, field) each member has listed.
SEEDED = {
    "A": [("cy", "email"), ("cy", "device_id"), ("dee", "shipping_address"), ("hal", "email")],
    "B": [
        ("ben", "email"),
        ("fay", "shipping_address"),
        ("gus", "email"),
        ("gus", "shipping_address"),
        ("gus", "device_id"),
        ("hal", "email"),
    ],
    "C": [
        ("cy", "email"),
        ("cy", "device_id"),
        ("eve", "device_id"),
        ("fay", "shipping_address"),
        ("hal", "email"),
    ],
}


def seeded_hashes() -> dict[str, list[str]]:
    return {m: [identifier_hash(f, PEOPLE[p][f]) for p, f in rows] for m, rows in SEEDED.items()}


def person(name: str, **overrides) -> dict:
    return {**PEOPLE[name], **overrides}


def dee_messy_address() -> dict:
    return {
        "address_line_1": "  12  HARBOUR st ",
        "address_line_2": "apt. 4",
        "admin_area_2": "SPRINGFIELD",
        "admin_area_1": "il",
        "postal_code": "627 01",
        "country_code": " us",
    }


# (name, identifiers, expected hits as {(member, field)})
CASES = [
    ("clean", person("ana"), set()),
    ("one email hit", person("ben"), {("B", "email")}),
    (
        "email and device at two members",
        person("cy"),
        {("A", "email"), ("A", "device_id"), ("C", "email"), ("C", "device_id")},
    ),
    (
        "email case and whitespace",
        person("ben", email="  BEN.Buyer@Example.COM\t"),
        {("B", "email")},
    ),
    (
        "device case and whitespace",
        person("cy", device_id=" DEV-CY "),
        {("A", "email"), ("A", "device_id"), ("C", "email"), ("C", "device_id")},
    ),
    ("address hit", person("dee"), {("A", "shipping_address")}),
    (
        "address normalisation",
        person("dee", shipping_address=dee_messy_address()),
        {("A", "shipping_address")},
    ),
    (
        "address NFKC fullwidth",
        person("dee", shipping_address=address("１２ Harbour St", "Apt 4")),
        {("A", "shipping_address")},
    ),
    ("device hit only", person("eve"), {("C", "device_id")}),
    (
        "same address at two members",
        person("fay"),
        {("B", "shipping_address"), ("C", "shipping_address")},
    ),
    (
        "all three fields at one member",
        person("gus"),
        {("B", "email"), ("B", "shipping_address"), ("B", "device_id")},
    ),
    ("email at all three members", person("hal"), {("A", "email"), ("B", "email"), ("C", "email")}),
    ("value in the wrong field", person("ana", email="dev-eve"), set()),
    ("no Gmail-style dot removal", person("ben", email="benbuyer@example.com"), set()),
    ("no plus-tag removal", person("ben", email="ben.buyer+shop@example.com"), set()),
    (
        "different apartment",
        person("dee", shipping_address=address("12 Harbour St.", "Apt 5")),
        set(),
    ),
    (
        "different country",
        person("dee", shipping_address=address("12 Harbour St.", "Apt 4", country="CA")),
        set(),
    ),
    ("near-miss device", person("eve", device_id="dev-eve-2"), set()),
    (
        "mixed hits from three people",
        person(
            "ana",
            email="ben.buyer@example.com",
            shipping_address=PEOPLE["dee"]["shipping_address"],
            device_id="dev-eve",
        ),
        {("B", "email"), ("A", "shipping_address"), ("C", "device_id")},
    ),
    (
        "missing address parts",
        person("ana", shipping_address={"address_line_1": "1 Elm St", "country_code": "US"}),
        set(),
    ),
]


def sensitive_values() -> list[str]:
    """Everything that must never reach a log: raw-ish, normalised and hashed identifiers."""
    out: set[str] = set()
    for _name, ids, _hits in CASES:
        for field, value in ids.items():
            out.add(normalise(field, value))
            out.add(identifier_hash(field, value))
            if isinstance(value, str):
                out.add(value.strip())
    return sorted(v for v in out if len(v) >= 6)
