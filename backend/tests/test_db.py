from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.db import make_engine, receipts


def test_tables_created(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    names = set(inspect(engine).get_table_names())
    assert names == {"orders", "checks", "receipts", "decisions", "events", "disputes"}


def test_json_columns_are_jsonb_on_postgres() -> None:
    ddl = str(CreateTable(receipts).compile(dialect=postgresql.dialect()))
    assert "intent_directives JSONB" in ddl
    assert "JSONB" not in str(CreateTable(receipts).compile(dialect=sqlite.dialect()))


def test_postgres_urls_use_psycopg() -> None:
    from sqlalchemy import create_engine

    from app import db

    seen = []
    real = db.create_engine
    db.create_engine = lambda url: seen.append(url) or create_engine("sqlite://")
    try:
        make_engine("postgres://u:p@host/db")
        make_engine("postgresql://u:p@host/db")
    finally:
        db.create_engine = real
    assert seen == ["postgresql+psycopg://u:p@host/db"] * 2
