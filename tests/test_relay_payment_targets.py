"""Signed Amethyst/Wisp NIP-A3 events through the relay's real storage rules."""
import asyncio
import json
import sqlite3

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.store import RelayStore
from app.services.nostr_relay.server import RelayServer, _broadcastable, _matches
from tests.test_relay_prune import store_factory, _run


@pytest.fixture
def relay():
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.executescript('''CREATE TABLE events(id TEXT PRIMARY KEY, pubkey TEXT,
      created_at INTEGER, kind INTEGER, content TEXT, tags TEXT, sig TEXT, raw TEXT,
      origin TEXT, expiration INTEGER);
      CREATE TABLE event_tags(event_id TEXT,tag TEXT,value TEXT,PRIMARY KEY(event_id,tag,value));''')
    store=RelayStore.__new__(RelayStore)
    yield store,conn
    conn.close()


def signed(tags,stamp=100,key=b'\x11'*32,kind=10133):
    return build_event(key,kind,'',tags,created_at=stamp)


@pytest.mark.parametrize('origin',['wot','direct'])
@pytest.mark.parametrize('reverse',[False,True])
def test_same_second_targets_converge_by_event_id(relay,origin,reverse):
    store,db=relay
    events=[signed([['payto','lightning','alice@one.test']]),signed([['payto','monero','8'+'a'*94]])]
    for event in events[::(-1 if reverse else 1)]:store._insert_one(db,event,origin)
    assert [r[0] for r in db.execute('SELECT id FROM events')]==[min(e['id'] for e in events)]


@pytest.mark.parametrize('origin',['wot','direct'])
def test_new_targets_and_explicit_clear_replace_previous_lists(relay,origin):
    store,db=relay
    old=signed([['payto','lightning','alice@old.test'],['payto','monero','8'+'a'*94]])
    new=signed([['payto','bitcoin','bc1qtest'],['payto','newnetwork','someuser']],101)
    clear=signed([['alt','Payment targets']],102)
    for event in (old,new,clear):assert store._insert_one(db,event,origin)
    assert not store._insert_one(db,old,origin)
    assert not store._insert_one(db,new,origin)
    got=json.loads(db.execute('SELECT raw FROM events WHERE kind=10133').fetchone()[0])
    assert got==clear


def test_authors_are_independent_and_complete_payment_tags_are_retained(relay):
    store,db=relay
    for key in (b'\x11'*32,b'\x22'*32):
        assert store._insert_one(db,signed([['payto','monero','8'+'a'*94]],key=key),'direct')
    assert db.execute('SELECT count(*) FROM events WHERE kind=10133').fetchone()[0]==2
    # NIP-01 indexes single-letter tags. NIP-A3 is discovered by author + kind;
    # the full multi-letter payto tags must survive the event's serialized body.
    for result in db.execute('SELECT raw FROM events WHERE kind=10133'):
        assert json.loads(result[0])['tags']==[['payto','monero','8'+'a'*94]]


def test_deleted_target_is_not_resurrected_by_backfill(relay):
    store,db=relay;event=signed([['payto','lightning','alice@old.test']])
    assert store._insert_one(db,event,'direct')
    assert store._insert_one(db,signed([['e',event['id']]],101,kind=5),'direct')
    assert not store._insert_one(db,event,'wot')
    assert not db.execute('SELECT id FROM events WHERE kind=10133').fetchall()
    assert store._insert_one(db,signed([['payto','lightning','alice@new.test']],102),'wot')


def test_another_author_cannot_delete_payment_targets(relay):
    store,db=relay;event=signed([['payto','lightning','alice@test.test']])
    assert store._insert_one(db,event,'direct')
    store._insert_one(db,signed([['e',event['id']]],101,key=b'\x22'*32,kind=5),'wot')
    assert db.execute('SELECT id FROM events WHERE kind=10133').fetchone()[0]==event['id']


def test_targets_can_be_broadcast_to_other_clients():
    assert _broadcastable(signed([['payto','monero','8'+'a'*94]]),None)


def test_signed_publish_live_subscription_reload_and_forgery(relay):
    store,db=relay
    class Disk:
        async def has_event(self,eid):
            return bool(db.execute('SELECT id FROM events WHERE id=?',(eid,)).fetchone())
        async def add_event(self,event,origin='direct'):
            return store._insert_one(db,event,origin)
        async def query(self,filters):
            return [json.loads(r[0]) for r in db.execute('SELECT raw FROM events ORDER BY created_at DESC')
                    if _matches(filters,json.loads(r[0]))]
    class Gate:
        def is_member(self,_):return True
        def is_operator(self,_):return False
        def is_puppet_event(self,_):return False
        def is_blocked(self,_):return False
    server=RelayServer(Disk(),Gate(),{'wot_enabled':True})
    sent=[];server._send=lambda conn,frame:sent.append((conn,frame))
    publisher,reader=object(),object()
    event=signed([['payto','monero','8'+'a'*94]])
    async def run():
        await server._on_req(reader,'targets',[{'authors':[event['pubkey']],'kinds':[10133]}])
        assert sent==[(reader,['EOSE','targets'])]
        sent.clear();await server._on_event(publisher,event)
        assert (publisher,['OK',event['id'],True,'']) in sent
        assert (reader,['EVENT','targets',event]) in sent
        sent.clear();await server._on_req(reader,'reload',[{'authors':[event['pubkey']],'kinds':[10133]}])
        assert sent==[(reader,['EVENT','reload',event]),(reader,['EOSE','reload'])]
        forged=json.loads(json.dumps(event));forged['tags'][0][2]='4'+'b'*94
        sent.clear();await server._on_event(publisher,forged)
        assert len(sent)==1 and sent[0][1][:3]==['OK',event['id'],False]
        assert json.loads(db.execute('SELECT raw FROM events WHERE kind=10133').fetchone()[0])==event
    asyncio.run(run())


def test_real_postgres_query_and_retention_keep_targets(store_factory):
    async def run(loop):
        store=store_factory(loop,retention_days=1)
        event=signed([['payto','monero','8'+'a'*94]])
        assert await store.add_event(event,origin='direct')
        assert await store.query([{'authors':[event['pubkey']],'kinds':[10133],'limit':1}])==[event]
        preview=await store.prune_preview()
        assert preview['total']==0
        assert await store.prune()==0
        clear=signed([['alt','Payment targets']],101)
        assert await store.add_event(clear,origin='wot')
        assert await store.query([{'authors':[event['pubkey']],'kinds':[10133]}])==[clear]
    _run(run)
