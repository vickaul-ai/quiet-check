"""Identifier normalisation and hashing, shared by the risk service and every member.

Rules: docs/contracts.md, "Identifier normalisation" (ADR 0003). Never log the values this
module produces.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping

FIELDS = ("email", "shipping_address", "device_id")

ADDRESS_PARTS = ("address_line_1", "address_line_2", "admin_area_2", "admin_area_1", "postal_code")

_WHITESPACE = re.compile(r"\s+")


def _strip_punctuation(value: str) -> str:
    return "".join(ch for ch in value if not unicodedata.category(ch).startswith("P"))


def _address_part(value: str | None) -> str:
    # In the contract's order: NFKC, lowercase, trim, collapse whitespace, remove punctuation.
    value = unicodedata.normalize("NFKC", value or "").lower().strip()
    return _strip_punctuation(_WHITESPACE.sub(" ", value))


def normalise_email(value: str) -> str:
    return value.strip().lower()


def normalise_device_id(value: str) -> str:
    return value.strip().lower()


def normalise_shipping_address(address: Mapping[str, str | None]) -> str:
    parts = [_address_part(address.get(name)) for name in ADDRESS_PARTS]
    parts[-1] = parts[-1].replace(" ", "")  # postal_code: no spaces at all
    parts.append((address.get("country_code") or "").strip().upper())
    return "|".join(parts)


def normalise(field: str, value) -> str:
    if field == "email":
        return normalise_email(value)
    if field == "shipping_address":
        return normalise_shipping_address(value)
    if field == "device_id":
        return normalise_device_id(value)
    raise ValueError(f"unknown field: {field}")


def hash_normalised(field: str, normalised: str) -> str:
    if field not in FIELDS:
        raise ValueError(f"unknown field: {field}")
    return hashlib.sha256(f"{field}:{normalised}".encode()).hexdigest()


def identifier_hash(field: str, value) -> str:
    """Normalise, then hash: the value that enters PSI or a member's list file."""
    return hash_normalised(field, normalise(field, value))
