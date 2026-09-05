"""Deleted social activity must not return through relay synchronization."""
import sqlite3

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.store import RelayStore


@pytest.fixture
def store():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE events(id TEXT PRIMARY KEY, pubkey TEXT, created_at INTEGER,
            kind INTEGER, content TEXT, tags TEXT, sig TEXT, raw TEXT, origin TEXT, expiration INTEGER);
        CREATE TABLE event_tags(event_id TEXT, tag TEXT, value TEXT,
            PRIMARY KEY(event_id, tag, value));
    ''')
    relay = RelayStore.__new__(RelayStore)
    yield lambda ev: relay._insert_one(conn, ev, 'wot'), conn
    conn.close()


def signed(kind, tags=None, key=b'\x11' * 32):
    return build_event(key, kind, '', tags or [])


@pytest.mark.parametrize('kind', [1, 6, 7, 16])
@pytest.mark.parametrize('deletion_first', [False, True])
def test_deleted_event_cannot_be_imported_again(store, kind, deletion_first):
    put, conn = store
    original = signed(kind)
    deletion = signed(5, [['e', original['id']]])
    if not deletion_first:
        assert put(original)
    assert put(deletion)
    assert not put(original)
    assert conn.execute('SELECT id FROM events WHERE id=?', (original['id'],)).fetchone() is None


def test_another_authors_deletion_cannot_block_import(store):
    put, _ = store
    original = signed(7)
    assert put(signed(5, [['e', original['id']]], key=b'\x22' * 32))
    assert put(original)


def test_deletion_request_cannot_be_deleted_to_reenable_import(store):
    put, conn = store
    original = signed(6)
    deletion = signed(5, [['e', original['id']]])
    assert put(deletion)
    assert put(signed(5, [['e', deletion['id']]]))
    assert conn.execute('SELECT id FROM events WHERE id=?', (deletion['id'],)).fetchone()
    assert not put(original)


def test_giftwrap_keeps_its_existing_deletion_rules(store):
    put, _ = store
    original = signed(1059)
    assert put(signed(5, [['e', original['id']]]))
    assert put(original)
