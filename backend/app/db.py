"""Database tables (SQLAlchemy 2.x Core, sync engine). See ADR 0005 and docs/contracts.md.

Tables are created with `metadata.create_all` at startup; there are no migrations yet.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    event,
)
from sqlalchemy.dialects import postgresql

# JSON on SQLite, JSONB on Postgres.
JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")


def Timestamp() -> DateTime:
    return DateTime(timezone=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


metadata = MetaData()

orders = Table(
    "orders",
    metadata,
    Column("id", Text, primary_key=True),
    Column("paypal_order_id", Text, unique=True, nullable=False),
    Column("status", Text, nullable=False),
    Column("items", JSONType, nullable=False),
    Column("amount_value", Text, nullable=False),
    Column("currency_code", Text, nullable=False),
    Column("device_id", Text, nullable=False),
    Column("payer_email", Text),
    Column("shipping_address", JSONType),
    Column("authorization_id", Text),
    Column("authorization_expires_at", Timestamp()),
    Column("honor_period_ends_at", Timestamp()),
    Column("capture_id", Text),
    Column("created_at", Timestamp(), nullable=False, default=utcnow),
    Column("updated_at", Timestamp(), nullable=False, default=utcnow, onupdate=utcnow),
)

checks = Table(
    "checks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, ForeignKey("orders.id"), unique=True, nullable=False),
    Column("status", Text, nullable=False),
    Column("hit_count", Integer),
    Column("error", Text),
    Column("started_at", Timestamp(), nullable=False, default=utcnow),
    Column("completed_at", Timestamp()),
)

receipts = Table(
    "receipts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("check_id", Text, ForeignKey("checks.id"), nullable=False, index=True),
    Column("member", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("is_match", Boolean, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("intent_directives", JSONType, nullable=False),
    Column("result_directive", JSONType, nullable=False),
    Column("member_commitment", JSONType, nullable=False),
    Column("member_public_key", Text, nullable=False),
    Column("verified", Boolean, nullable=False),
    Column("created_at", Timestamp(), nullable=False, default=utcnow),
)

decisions = Table(
    "decisions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, ForeignKey("orders.id"), unique=True, nullable=False),
    Column("check_id", Text, ForeignKey("checks.id"), nullable=False),
    Column("decision", Text, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("paypal_action", Text, nullable=False),
    Column("resolution", Text),
    Column("resolution_note", Text),
    Column("resolved_at", Timestamp()),
    Column("created_at", Timestamp(), nullable=False, default=utcnow),
)

events = Table(
    "events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("paypal_event_id", Text, unique=True),
    Column("event_type", Text, nullable=False),
    Column("order_id", Text, ForeignKey("orders.id")),
    Column("resource_id", Text),
    Column("verified", Boolean),
    Column("payload", JSONType, nullable=False),
    Column("created_at", Timestamp(), nullable=False, default=utcnow),
)

# One row per PayPal dispute (D8). `id` is PayPal's dispute ID; `order_id` is null when the
# dispute matched none of our orders. The draft is written by the model (or the template) and
# only a reviewer submits it.
disputes = Table(
    "disputes",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, ForeignKey("orders.id"), index=True),
    Column("reason", Text),
    Column("status", Text),
    Column("amount_value", Text),
    Column("currency_code", Text),
    Column("seller_response_due_date", Timestamp()),
    Column("draft_note", Text),
    Column("draft_source", Text),  # "llm", "template", or "pending" while a draft is written
    Column("drafted_at", Timestamp()),
    Column("submitted_note", Text),
    Column("submitted_at", Timestamp()),
    Column("submitted_by", Text),
    Column("created_at", Timestamp(), nullable=False, default=utcnow),
    Column("updated_at", Timestamp(), nullable=False, default=utcnow, onupdate=utcnow),
)


def make_engine(database_url: str) -> Engine:
    """Create the engine and the tables. `postgresql://` URLs use psycopg 3."""
    if database_url.startswith("postgres://"):  # Render-style URL
        database_url = "postgresql://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    engine = create_engine(database_url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_foreign_keys(dbapi_connection, _record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    return engine
