"""The reported Ditto quote had a valid q recipient and no p tag."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.nostr.event import verify_event
from app.services.nostr.quotes import quote_pubkeys
from app.services.nostr_relay.server import _matches
from app.services import nostr_push_service as push
from tests.test_relay_prune import store_factory, _run

QUOTE = json.loads((Path(__file__).parent / 'fixtures/nostr/ditto_quote_no_p.json').read_text())
OWNER = QUOTE['tags'][0][3]
FILTER = {'kinds': [1], '#p': [OWNER], '_include_quotes': True}


def test_reported_quote_signature_and_recipient():
    assert verify_event(QUOTE)
    assert not any(t[0] == 'p' for t in QUOTE['tags'])
    assert quote_pubkeys(QUOTE) == {OWNER}
    assert _matches([FILTER], QUOTE)
    assert not _matches([{'#p': [OWNER]}], QUOTE), 'standard #p must retain NIP-01 semantics'
    assert _matches([{'#q': [QUOTE['tags'][0][1]]}], QUOTE)
    assert not _matches([{**FILTER, 'since': QUOTE['created_at']+1}], QUOTE)
    assert not _matches([{**FILTER, '#p': ['a'*64]}], QUOTE)


@pytest.mark.parametrize('tags', [[], [['q']], [['q', 'f'*64, OWNER]],
    [['q', 'not-an-event', '', OWNER]], [['q', 'f'*64, '', 'bad']],
    [['_quote_author', OWNER]]])
def test_bad_or_unrelated_quote_tags_do_not_address_a_user(tags):
    ev = {**QUOTE, 'tags': tags}
    assert not quote_pubkeys(ev)
    assert not _matches([FILTER], ev)


def test_non_note_q_tag_does_not_gain_notification_semantics():
    ev = {**QUOTE, 'kind': 7}
    assert not quote_pubkeys(ev)
    assert not _matches([{'#p': [OWNER], '_include_quotes': True}], ev)


def test_postgres_index_backfill_and_new_events_agree_with_live_matching(store_factory):
    async def run(loop):
        store = store_factory(loop)
        assert await store.add_event(QUOTE)
        assert await store.query([FILTER]) == [QUOTE]
        assert await store.query([{'#p': [OWNER]}]) == []
        assert await store.query([{'#q': [QUOTE['tags'][0][1]]}]) == [QUOTE]
        # Simulate an existing database before the derived index existed.
        conn = store._conn()
        conn.execute("DELETE FROM event_tags WHERE tag='_quote_author'")
        conn.execute("DELETE FROM relay_kv WHERE key='quote_author_index_v1'")
        assert await store.query([FILTER]) == []
        store._index_existing_quotes(conn)
        store._index_existing_quotes(conn)
        assert await store.query([FILTER]) == [QUOTE]
        assert (await store.query([FILTER]))[0]['tags'] == QUOTE['tags']
        # A user-supplied multi-letter tag cannot impersonate the derived index.
        forged = {**QUOTE, 'id': 'f'*64, 'tags': [['_quote_author', OWNER]]}
        assert await store.add_event(forged)
        assert await store.query([FILTER]) == [QUOTE]
    _run(run)


@pytest.mark.parametrize('also_p', [False, True])
def test_poll_generates_one_notification_per_device_for_quote_recipient(monkeypatch, also_p):
    from app import database
    event = copy.deepcopy(QUOTE)
    if also_p:
        event['tags'] += [['p', OWNER], ['p', OWNER]]
    # A self-quote and duplicate q recipient must not duplicate or self-deliver.
    event['tags'] += [event['tags'][0][:], ['q', 'e'*64, '', QUOTE['pubkey']]]
    devices = [SimpleNamespace(id=i, pubkey=pk) for i, pk in enumerate(
        [OWNER, OWNER, QUOTE['pubkey'], 'a'*64])]
    db = SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: devices), close=lambda: None)
    monkeypatch.setattr(database, 'SessionLocal', lambda: db)
    monkeypatch.setattr(push, '_cursor', QUOTE['created_at']-1)
    monkeypatch.setattr(push, '_seen', set())
    monkeypatch.setattr(push, '_name_for', AsyncMock(return_value='Ditto author'))
    monkeypatch.setattr(push, 'subscription_dict', lambda s: {'id': s.id})
    monkeypatch.setattr(push, '_local_relay', lambda: ['ws://test.invalid'])
    async def query(urls, filters, **kwargs):
        return [event] if _matches(filters, event) else []
    monkeypatch.setattr(push.relay, 'query', query)
    sent = []
    monkeypatch.setattr(push.push_service, 'send', lambda sub, payload: sent.append((sub, dict(payload))) or True)
    asyncio.run(push._poll())
    asyncio.run(push._poll())
    assert [s['id'] for s, p in sent] == [0, 1]
    assert all(p['eid'] == QUOTE['id'] and p['body'] == 'Ditto author quoted your post' for s, p in sent)
