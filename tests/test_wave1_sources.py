"""The one-time copy's SQL readers for the wave-1 tables (scripts/nostr_migration/wave1_sources.py, #161).

The app is relay-only; these readers are the ONLY code that still imports the old SQLAlchemy models. Pinned:
every relay table a store reads has a reader under the store's own table name (a renamed store would otherwise
be silently skipped by the copy), and every reader runs against the real old schema.
"""
import pathlib
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.nostr_migration import wave1_sources as W  # noqa: E402


def test_every_store_table_has_a_reader_under_its_own_name():
    from app.services import (api_key_store, app_tables, blob_index, external_storage_store, fedi_tables,
                              push_store, share_store, stream_vod_store, verification_store)
    want = {api_key_store.TABLE, blob_index.TABLE, external_storage_store.TABLE, fedi_tables.PUPPETS,
            fedi_tables.LEDGER, share_store.TABLE, stream_vod_store.TABLE, verification_store.TABLE,
            push_store.SUBS, push_store.DIRECT, push_store.FOLLOWS} | set(app_tables.TABLES)
    assert want == set(W.SOURCES)
    assert set(W.DERIVED) == {api_key_store.INDEX}
    assert push_store.SENT not in W.SOURCES, "the 10-minute sent-wrap ledger is transient: never copied"


def test_every_reader_runs_on_the_old_schema():
    from app.database import Base
    import importlib
    importlib.import_module("app.models")             # registers the old tables
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    got = {name: fn(db) for name, fn in W.SOURCES.items()}
    assert all(rows == {} for rows in got.values()), got
    assert W.DERIVED["api_key_index"](got) == {}
