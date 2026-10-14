# 0005. Database layer: SQLAlchemy 2.x Core, sync engine
_Date: 2026-10-13 · Status: accepted_

## Context
D2 needs the `orders`, `checks` and `receipts` tables, and later deliverables use `decisions` and `events`. The same
code runs on SQLite locally and Postgres on Render (`DATABASE_URL`), and contracts.md wants JSON columns to be JSONB on
Postgres. The data model is small and fixed by contracts.md; most queries are single-row reads and conditional updates.

## Decision
- SQLAlchemy 2.x **Core** (tables and SQL expressions), not the ORM. Sync engine.
- Postgres through psycopg 3: `postgres://` and `postgresql://` URLs are rewritten to `postgresql+psycopg://`.
- All five tables live in `backend/app/db.py` and are created with `metadata.create_all` at startup. No Alembic yet.
- JSON columns use `JSON().with_variant(postgresql.JSONB(), "postgresql")`, the SQLAlchemy 2.0 form (`with_variant`
  replaces the deprecated `Variant` class; checked against the 2.0 type API docs on 2026-10-13). On SQLite the column is
  declared `JSON` and stored as text.
- SQLite connections turn on `PRAGMA foreign_keys=ON` so the FKs in contracts.md are enforced locally too.

Vic chose this before the D2 build session; this ADR records it.

## Alternatives considered
- Plain SQL (DB-API): no dependency, but two dialects' placeholder styles, JSON handling and DDL to maintain by hand.
- SQLAlchemy ORM: mapped classes add little for five tables accessed through a few functions.
- Async engine: the endpoints are async, but the queries are short; a sync engine keeps tests and scripts simple.

## Consequences
- One table definition serves both databases, and the conditional updates contracts.md needs (`WHERE status IN ...`)
  are plain Core expressions.
- Sync queries run on the event loop. They are short single-row statements; revisit if a query grows slow.
- Schema changes after the first Render deploy need care: `create_all` adds missing tables but never alters existing
  ones. Add Alembic (or drop and recreate the demo database) when a column changes.
