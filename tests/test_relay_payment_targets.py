"""Signed Amethyst/Wisp NIP-A3 events through the relay's real storage rules (its PosterChanDB store)."""
import asyncio
import json

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.server import RelayServer, _broadcastable
from tests.relay_backends import pcdb_factory
from tests.test_relay_prune import store_factory, _run  # noqa: F401 -- the fixture, by name


@pytest.fixture
def relay():
    loop = asyncio.new_event_loop()
    try:
        with pcdb_factory() as make:
            yield make(loop)
    finally:
        loop.close()


def stored(store, kind=10133):
    return store._query_sync([{'kinds': [kind]}], 5000)


def signed(tags,stamp=100,key=b'\x11'*32,kind=10133):
    return build_event(key,kind,'',tags,created_at=stamp)


@pytest.mark.parametrize('origin',['wot','direct'])
@pytest.mark.parametrize('reverse',[False,True])
def test_same_second_targets_converge_by_event_id(relay,origin,reverse):
    store=relay
    events=[signed([['payto','lightning','alice@one.test']]),signed([['payto','monero','8'+'a'*94]])]
    for event in events[::(-1 if reverse else 1)]:store._add_event_sync(event,origin)
    assert [e['id'] for e in stored(store)]==[min(e['id'] for e in events)]


@pytest.mark.parametrize('origin',['wot','direct'])
def test_new_targets_and_explicit_clear_replace_previous_lists(relay,origin):
    store=relay
    old=signed([['payto','lightning','alice@old.test'],['payto','monero','8'+'a'*94]])
    new=signed([['payto','bitcoin','bc1qtest'],['payto','newnetwork','someuser']],101)
    clear=signed([['alt','Payment targets']],102)
    for event in (old,new,clear):assert store._add_event_sync(event,origin)
    assert not store._add_event_sync(old,origin)
    assert not store._add_event_sync(new,origin)
    assert stored(store)==[clear]


def test_authors_are_independent_and_complete_payment_tags_are_retained(relay):
    store=relay
    for key in (b'\x11'*32,b'\x22'*32):
        assert store._add_event_sync(signed([['payto','monero','8'+'a'*94]],key=key),'direct')
    got=stored(store)
    assert len(got)==2
    # NIP-01 indexes single-letter tags. NIP-A3 is discovered by author + kind;
    # the full multi-letter payto tags must survive the event's serialized body.
    for event in got:
        assert event['tags']==[['payto','monero','8'+'a'*94]]


def test_deleted_target_is_not_resurrected_by_backfill(relay):
    store=relay;event=signed([['payto','lightning','alice@old.test']])
    assert store._add_event_sync(event,'direct')
    assert store._add_event_sync(signed([['e',event['id']]],101,kind=5),'direct')
    assert not store._add_event_sync(event,'wot')
    assert not stored(store)
    assert store._add_event_sync(signed([['payto','lightning','alice@new.test']],102),'wot')


def test_another_author_cannot_delete_payment_targets(relay):
    store=relay;event=signed([['payto','lightning','alice@test.test']])
    assert store._add_event_sync(event,'direct')
    store._add_event_sync(signed([['e',event['id']]],101,key=b'\x22'*32,kind=5),'wot')
    assert [e['id'] for e in stored(store)]==[event['id']]


def test_targets_can_be_broadcast_to_other_clients():
    assert _broadcastable(signed([['payto','monero','8'+'a'*94]]),None)


def test_signed_publish_live_subscription_reload_and_forgery(relay):
    store=relay
    class Disk:
        async def has_event(self,eid):
            return store._has_sync(eid)
        async def add_event(self,event,origin='direct'):
            return store._add_event_sync(event,origin)
        async def query(self,filters):
            return store._query_sync(filters,5000)
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
        assert stored(store)==[event]
    asyncio.run(run())


def test_the_relay_query_and_retention_keep_targets(store_factory):
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
