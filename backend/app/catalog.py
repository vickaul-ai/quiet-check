"""The product catalogue: `catalog.json` is the source of truth, one SKU per variant.

The backend prices orders from it and never trusts prices from the shopping agent. The web app
reads it through `GET /catalog`.
"""

import json
from decimal import Decimal
from functools import cache
from pathlib import Path

from fastapi import APIRouter

CATALOG_PATH = Path(__file__).with_name("catalog.json")

router = APIRouter(tags=["catalog"])


@cache
def load_catalog() -> dict[str, dict]:
    items = json.loads(CATALOG_PATH.read_text())
    for item in items:
        assert item["unit_amount"]["currency_code"] == "USD", item["sku"]
        Decimal(item["unit_amount"]["value"])  # must parse
    return {item["sku"]: item for item in items}


@router.get("/catalog")
def get_catalog() -> dict:
    return {"items": list(load_catalog().values())}
