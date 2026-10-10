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
from tests.relay_backends import is_pcdb

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
        # Simulate an existing database before the derived index existed. (Postgres only: the one-time
        # migration has no PosterChanDB counterpart -- that store derives the index at ingest and on copy.)
        if not is_pcdb(store):
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


# ── Quotes whose q tag names NO author ─────────────────────────────────────────────────────────
# NIP-18 makes q[3] optional and many clients send `["q", <id>]`. Reported: "i did not get a
# notification that someone quote posted my post". Measured, 2 of 7 quotes of the reporter's posts
# had no author in the q tag: one with a p tag (worded "mentioned you"), one would reach nobody.
from app.services.nostr import quotes as quotes_mod
from app.services.nostr.event import build_event
from app.services import push_prefs

ALICE_SK, BOB_SK = bytes([7]) * 32, bytes([9]) * 32


def _bare_quote(target, *, p_tag=False, sk=BOB_SK):
    tags = [['q', target['id']]] + ([['p', target['pubkey']]] if p_tag else [])
    return build_event(sk, 1, 'look at this nostr:note1…', tags)


@pytest.fixture(autouse=True)
def _clean_resolved():
    getattr(quotes_mod, '_RESOLVED', {}).clear()
    yield
    getattr(quotes_mod, '_RESOLVED', {}).clear()


def test_a_bare_q_tag_resolves_to_the_quoted_posts_author():
    post = build_event(ALICE_SK, 1, 'my post', [])
    q = _bare_quote(post)
    assert quote_pubkeys(q) == set(), 'nothing to go on without a lookup'
    assert quotes_mod.quoted_ids_without_author(q) == [post['id']]
    assert quote_pubkeys(q, {post['id']: post['pubkey']}) == {post['pubkey']}
    # A NAMED author is what the tag says; a lookup never overrides it.
    named = {**q, 'tags': [['q', post['id'], '', 'c' * 64]]}
    assert quotes_mod.quoted_ids_without_author(named) == []
    assert quote_pubkeys(named, {post['id']: post['pubkey']}) == {'c' * 64}


@pytest.mark.parametrize('order', ['post_first', 'quote_first'])
def test_store_indexes_a_bare_quote_for_the_quoted_author_and_live_fanout_matches(store_factory, order):
    post = build_event(ALICE_SK, 1, 'my post', [])
    q = _bare_quote(post)
    flt = {'kinds': [1], '#p': [post['pubkey']], '_include_quotes': True}

    async def run(loop):
        store = store_factory(loop)
        for ev in ([post, q] if order == 'post_first' else [q, post]):
            assert await store.add_event(ev)
        assert [e['id'] for e in await store.query([flt])] == [q['id']]
        assert await store.query([{'kinds': [1], '#p': [post['pubkey']]}]) == [], 'plain #p unchanged'
        if order == 'post_first':
            # The relay's live fan-out runs right after the insert, against the same process.
            assert _matches([flt], q)
    _run(run)


def test_backfill_v2_indexes_existing_bare_quotes(store_factory):
    post = build_event(ALICE_SK, 1, 'my post', [])
    q = _bare_quote(post)
    flt = {'kinds': [1], '#p': [post['pubkey']], '_include_quotes': True}

    async def run(loop):
        store = store_factory(loop)
        assert await store.add_event(post) and await store.add_event(q)
        if is_pcdb(store):     # no migration to run: the index is derived at ingest
            assert [e['id'] for e in await store.query([flt])] == [q['id']]
            return
        conn = store._conn()
        conn.execute("DELETE FROM event_tags WHERE tag='_quote_author'")
        # The state every LIVE database is in: v1 already ran, v2 never has.
        conn.execute("DELETE FROM relay_kv WHERE key='quote_author_index_v2'")
        conn.execute("INSERT INTO relay_kv (key,value) VALUES ('quote_author_index_v1','1') ON CONFLICT DO NOTHING")
        assert await store.query([flt]) == []
        store._index_existing_quotes(conn)
        store._index_existing_quotes(conn)
        assert [e['id'] for e in await store.query([flt])] == [q['id']]
    _run(run)


@pytest.mark.parametrize('p_tag', [False, True])
def test_push_says_quoted_your_post_for_a_bare_quote(monkeypatch, p_tag):
    from app import database
    post = build_event(ALICE_SK, 1, 'my post', [])
    q = _bare_quote(post, p_tag=p_tag)
    owner = post['pubkey']
    devices = [SimpleNamespace(id=1, pubkey=owner)]
    db = SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: devices), close=lambda: None)
    monkeypatch.setattr(database, 'SessionLocal', lambda: db)
    monkeypatch.setattr(push, '_cursor', q['created_at'] - 1)
    monkeypatch.setattr(push, '_seen', set())
    monkeypatch.setattr(push, '_name_for', AsyncMock(return_value='Bob'))
    monkeypatch.setattr(push, 'subscription_dict', lambda s: {'id': s.id})
    monkeypatch.setattr(push, '_local_relay', lambda: ['ws://test.invalid'])

    async def query(urls, filters, **kwargs):
        if any('ids' in f for f in filters):          # the quoted post, looked up by id
            return [post] if any(post['id'] in f.get('ids', []) for f in filters) else []
        # What the relay's derived index answers for `#p` + `_include_quotes`.
        hit = p_tag or any(owner in f.get('#p', []) and f.get('_include_quotes') for f in filters)
        return [q] if hit and any(f.get('kinds') != [3] for f in filters) else []
    monkeypatch.setattr(push.relay, 'query', query)
    sent = []
    monkeypatch.setattr(push.push_service, 'send', lambda sub, payload: sent.append(dict(payload)) or True)
    asyncio.run(push._poll())
    assert len(sent) == 1, sent
    assert sent[0]['body'] == 'Bob quoted your post'
    assert sent[0]['type'] == 'quotes'
    assert push_prefs.push_type(q, owner, {owner}) == 'quotes'
