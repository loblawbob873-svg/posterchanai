"""Deleted social activity must not return through relay synchronization."""
import asyncio

import pytest

from app.services.nostr.event import build_event
from tests.relay_backends import pcdb_factory


class _Held:
    """What the store holds, asked through its own query path."""
    def __init__(self, st):
        self.st = st

    def has(self, eid):
        return self.st._has_sync(eid)

    def count(self):
        return len(self.st._query_sync([{"limit": 5000}], 5000))


@pytest.fixture
def store():
    loop = asyncio.new_event_loop()
    try:
        with pcdb_factory() as make:
            st = make(loop)
            yield (lambda ev: st._add_event_sync(ev, 'wot')), _Held(st)
    finally:
        loop.close()


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
    assert not conn.has(original['id'])


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
    assert conn.has(deletion['id'])
    assert not put(original)


def test_giftwrap_keeps_its_existing_deletion_rules(store):
    put, _ = store
    original = signed(1059)
    assert put(signed(5, [['e', original['id']]]))
    assert put(original)


@pytest.mark.parametrize('kind', [1, 5, 6, 7, 16, 30078])
def test_private_fediverse_action_is_never_stored_on_public_relay(store, kind):
    put, conn = store
    ev = signed(kind, [['client-mode', 'fedi-only']])
    assert not put(ev)
    assert conn.count() == 0
