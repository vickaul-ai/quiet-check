"""Our IDs: a prefix plus a ULID (`ord_`, `chk_`, `rcp_`, `dec_`, `evt_`)."""

from ulid import ULID


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"
