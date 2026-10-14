import hashlib

import pytest

from app.identifiers import (
    hash_normalised,
    identifier_hash,
    normalise_device_id,
    normalise_email,
    normalise_shipping_address,
)

ADDRESS = {
    "address_line_1": "12 Harbour St.",
    "address_line_2": "Apt 4",
    "admin_area_2": "Springfield",
    "admin_area_1": "IL",
    "postal_code": "62701",
    "country_code": "US",
}


def test_contract_example() -> None:
    expected = hashlib.sha256(b"email:ben.buyer@example.com").hexdigest()
    assert normalise_email(" Ben.Buyer@Example.com ") == "ben.buyer@example.com"
    assert identifier_hash("email", " Ben.Buyer@Example.com ") == expected


def test_email_keeps_dots_and_plus_tags() -> None:
    assert normalise_email("B.En+Shop@Gmail.com") == "b.en+shop@gmail.com"
    assert identifier_hash("email", "ben@gmail.com") != identifier_hash("email", "b.en@gmail.com")


def test_device_id() -> None:
    assert normalise_device_id("  DEV-Ben-91C2\n") == "dev-ben-91c2"


def test_address_basic() -> None:
    assert normalise_shipping_address(ADDRESS) == "12 harbour st|apt 4|springfield|il|62701|US"


def test_address_whitespace_case_punctuation_and_postal_spaces() -> None:
    messy = {
        "address_line_1": "  12   HARBOUR\tSt.,  ",
        "address_line_2": "Apt. 4",
        "admin_area_2": "springfield",
        "admin_area_1": "I.L.",
        "postal_code": " 627 01 ",
        "country_code": " us ",
    }
    assert normalise_shipping_address(messy) == normalise_shipping_address(ADDRESS)


def test_address_nfkc() -> None:
    fullwidth = dict(ADDRESS, address_line_1="１２ Ｈａｒｂｏｕｒ St.")
    assert normalise_shipping_address(fullwidth) == normalise_shipping_address(ADDRESS)


def test_address_missing_parts_become_empty() -> None:
    sparse = {"address_line_1": "1 Main St", "country_code": "GB"}
    assert normalise_shipping_address(sparse) == "1 main st|||||GB"
    assert normalise_shipping_address(dict(sparse, address_line_2=None)) == "1 main st|||||GB"


def test_address_rule_order_is_literal() -> None:
    # Whitespace is collapsed before punctuation is removed, as contracts.md lists the steps.
    assert normalise_shipping_address({"address_line_1": "St . 4"}).startswith("st  4|")


def test_field_prefix_separates_fields() -> None:
    assert identifier_hash("email", "x") != identifier_hash("device_id", "x")
    assert hash_normalised("device_id", "x") == hashlib.sha256(b"device_id:x").hexdigest()


def test_hash_is_lowercase_hex() -> None:
    value = identifier_hash("shipping_address", ADDRESS)
    assert len(value) == 64 and value == value.lower()


def test_unknown_field() -> None:
    with pytest.raises(ValueError):
        identifier_hash("phone", "1")
