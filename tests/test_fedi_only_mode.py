"""HTTP route → real writeback handler → simulated Mastodon HTTP API, with an isolated DB."""
import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.database import get_db
from app.models import Base, User, UserSetting, FediBridgeDelivered, FediBridgeAction, FediOnlyEvent
from app.routers.pleroma import router
from app.services import fedi_only_service as mode
from app.services import fedi_nostr_writeback_service as wb
from app.services import fedi_nostr_bridge_service as mirror
from app.services.nostr.event import build_event
from app.services.nostr.nostr_service import npub_from_seckey

SK = b'\x31' * 32
INST = 'https://fedi.test'


@pytest.fixture(params=['sqlite', 'postgres'])
def world(monkeypatch, request):
    admin = None
    if request.param == 'postgres':
        admin = create_engine('postgresql+psycopg2://posterchan@127.0.0.1:5432/posterchan_relay',connect_args={'connect_timeout':3})
        schema='fedi_only_test_'+uuid.uuid4().hex
        try:
            with admin.begin() as c: c.execute(text('CREATE SCHEMA '+schema))
        except Exception:
            admin.dispose()
            pytest.skip('PostgreSQL unavailable for the isolated integration schema')
        engine=create_engine(admin.url,connect_args={'options':'-csearch_path='+schema})
    else:
        engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine, tables=[User.__table__, UserSetting.__table__, FediBridgeDelivered.__table__, FediBridgeAction.__table__, FediOnlyEvent.__table__])
    db = sessionmaker(bind=engine)()
    user = User(username='test-user', password_hash='unused', nostr_npub=npub_from_seckey(SK.hex()),
                fedi_only=True, fedi_crosspost_enabled=False, fedi_bridge_enabled=False,
                pleroma_instance_url=INST, pleroma_access_token='test-token', pleroma_acct='tester@fedi.test')
    db.add(user); db.commit()
    calls = []
    fail = {'on': False}
    def mastodon(req):
        calls.append(req)
        if fail['on']:
            return httpx.Response(503, json={'error': 'test outage'})
        if req.url.host == 'media.test':
            return httpx.Response(200,content=b'\x89PNG\r\n\x1a\n' + b'test image',headers={'Content-Type':'image/png'})
        assert req.url.host == 'fedi.test', 'unexpected outbound host'
        assert req.headers.get('Authorization') == 'Bearer test-token'
        path = req.url.path
        if path in ('/api/v1/media','/api/v2/media'):
            return httpx.Response(200,json={'id':'media-1'})
        if '/reactions/' in path:
            return httpx.Response(200,json={'id':'parent'})
        if path.endswith('/verify_credentials'):
            return httpx.Response(200, json={'id': 'me', 'username': 'tester', 'acct': 'tester@fedi.test'})
        if path == '/api/v1/statuses' and req.method == 'POST':
            body = json.loads(req.content)
            sid = str(100 + len(calls))
            return httpx.Response(200, json={'id': sid, 'uri': INST+'/objects/'+sid, **body})
        if path.startswith('/api/v1/statuses/'):
            return httpx.Response(200, json={'id': path.split('/')[4], 'visibility': 'unlisted',
                'account': {'acct': 'parent@fedi.test'}, 'mentions': [{'acct': 'friend@fedi.test'}]})
        raise AssertionError('unhandled Mastodon request: '+str(req.url))
    monkeypatch.setattr(wb.pleroma_service, 'afallback_transport', lambda: httpx.MockTransport(mastodon))
    real_client=httpx.AsyncClient
    def client(*args,**kwargs):
        kwargs.setdefault('transport',httpx.MockTransport(mastodon))
        return real_client(*args,**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',client)  # even media downloads cannot reach the network

    monkeypatch.setattr(wb, '_bridge_on', lambda: True)
    monkeypatch.setattr(wb, '_blocked_pubkeys', lambda: set())
    wb._seen_events.clear()
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    async def send(ev, **kw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://app.test') as c:
            r = await c.post('/api/pleroma/social-publish', json={'event': ev, **kw})
            assert r.status_code == 200, r.text
            return r.json()
    w = SimpleNamespace(db=db, user=user, calls=calls, fail=fail, send=send, app=app)
    yield w
    db.close(); engine.dispose(); wb._seen_events.clear()
    if admin is not None:
        with admin.begin() as c: c.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        admin.dispose()


def event(kind=1, content='hello', tags=(), private=True, sk=SK):
    return build_event(sk, kind, content, list(tags)+([mode.MARKER] if private else []))


def seed_parent(w):
    ev = event(1, private=False, sk=b'\x32'*32)
    w.db.add(FediBridgeDelivered(platform='pleroma', instance_url=INST, note_id='parent',
        note_uri=INST+'/objects/parent', author_acct='parent@fedi.test', nostr_event_id=ev['id'], nostr_pubkey=ev['pubkey']))
    w.db.commit()
    return ev


def test_top_level_posts_even_with_crosspost_disabled_and_retry_does_not_duplicate(world):
    ev = event()
    async def go():
        assert (await world.send(ev))['ok']
        wb._seen_events.clear()  # restart: use durable idempotency, not the process set
        assert (await world.send(ev))['ok']
    asyncio.run(go())
    posts = [r for r in world.calls if r.method == 'POST']
    assert len(posts) == 1
    assert json.loads(posts[0].content)['status'] == 'hello'
    assert posts[0].headers['Idempotency-Key'] == ev['id']
    assert world.db.query(FediBridgeDelivered).filter_by(nostr_event_id=ev['id']).one()


def test_reply_preserves_parent_visibility_and_thread_mentions(world):
    parent = seed_parent(world)
    result = asyncio.run(world.send(event(tags=[['e', parent['id'], '', 'reply']])))
    assert result['ok']
    body = json.loads([r for r in world.calls if r.method == 'POST'][-1].content)
    assert body['in_reply_to_id'] == 'parent'
    assert body['visibility'] == 'unlisted'
    assert '@parent@fedi.test' in body['status'] and '@friend@fedi.test' in body['status']


@pytest.mark.parametrize('kind,suffix', [(7, 'favourite'), (6, 'reblog'), (16, 'reblog')])
def test_reactions_and_reposts_reach_fediverse_once(world, kind, suffix):
    parent = seed_parent(world)
    ev = event(kind, '+', [['e',parent['id']]])
    async def go():
        assert (await world.send(ev))['ok']
        wb._seen_events.clear()
        assert (await world.send(ev))['ok']
    asyncio.run(go())
    assert len([r for r in world.calls if r.url.path.endswith('/'+suffix)]) == 1
    assert world.db.query(FediBridgeAction).filter_by(nostr_event_id=ev['id']).one()


def test_delete_undoes_private_post_and_reaction(world):
    parent = seed_parent(world)
    post, like = event(), event(7, '+', [['e',parent['id']]])
    async def go():
        assert (await world.send(post))['ok']
        assert (await world.send(like))['ok']
        assert (await world.send(event(5, '', [['e',post['id']],['e',like['id']]])))['ok']
    asyncio.run(go())
    assert any(r.method == 'DELETE' for r in world.calls)
    assert any(r.url.path.endswith('/unfavourite') for r in world.calls)
    assert world.db.query(FediBridgeDelivered).filter_by(nostr_event_id=post['id']).one().deleted_at
    assert world.db.query(FediBridgeAction).filter_by(nostr_event_id=like['id']).one().undone_at


def test_normal_mode_keeps_nostr_route_and_existing_crosspost_switch(world):
    world.user.fedi_only=False
    result=asyncio.run(world.send(event(private=False)))
    assert result == {'route':'nostr'}
    assert not world.calls
    assert world.user.fedi_crosspost_enabled is False


def test_mode_change_does_not_release_previously_private_posts_to_nostr(world):
    world.user.fedi_only=False
    assert asyncio.run(world.send(event()))['route']=='fediverse'
    assert len(world.calls)==1


@pytest.mark.parametrize('case',['forged','other-user','stale-client','unbridged-reply','unsupported','disconnected','outage','rebroadcast','bridge-off','blocked'])
def test_failures_never_fall_back_to_nostr(world, case, monkeypatch):
    ev=event(); kw={}
    if case=='forged': ev['content']='tampered'
    if case=='other-user': ev=event(sk=b'\x33'*32)
    if case=='stale-client': ev=event(private=False)
    if case=='unbridged-reply': ev=event(tags=[['e','f'*64,'','reply']])
    if case=='unsupported': ev=event(1068)
    if case=='disconnected': world.user.pleroma_access_token=None
    if case=='outage': world.fail['on']=True
    if case=='rebroadcast': kw['broadcast_only']=True
    if case=='bridge-off': monkeypatch.setattr(wb,'_bridge_on',lambda:False)
    if case=='blocked': monkeypatch.setattr(wb,'_blocked_pubkeys',lambda:{ev['pubkey']})
    result=asyncio.run(world.send(ev, **kw))
    assert result['route']=='fediverse' and result['ok'] is False
    if case!='outage': assert not world.calls


def test_our_mirror_does_not_publish_the_fediverse_only_author(world, monkeypatch):
    provision=AsyncMock(side_effect=AssertionError('private author reached public puppet publishing'))
    monkeypatch.setattr(mirror.ident,'ensure_puppet',provision)
    raw={'visibility':'public','account':{'acct':'tester','username':'tester','url':INST+'/@tester'}}
    result=asyncio.run(mirror._deliver(world.db,3052,'pleroma',INST,'fedi.test',raw,{'id':'private'}))
    assert result is None
    provision.assert_not_called()
    assert not mode.suppress_mirror(world.db,{'acct':'someone-else@fedi.test'},'fedi.test')


def test_http_route_requires_an_authenticated_session(world):
    del world.app.dependency_overrides[get_current_user]
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app),base_url='http://app.test') as c:
            return await c.post('/api/pleroma/social-publish',json={'event':event()})
    assert asyncio.run(go()).status_code in (401,403)
    assert not world.calls


def test_concurrent_retries_send_one_fediverse_status(world):
    ev=event()
    async def go():
        return await asyncio.gather(world.send(ev),world.send(ev))
    assert all(r['ok'] for r in asyncio.run(go()))
    assert len([r for r in world.calls if r.method=='POST'])==1


def test_private_history_is_owner_scoped_and_carries_deletions(world):
    ev=event()
    async def go():
        assert (await world.send(ev))['ok']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app),base_url='http://app.test') as c:
            first=(await c.get('/api/pleroma/private-events')).json()['events']
            assert [e['id'] for e in first]==[ev['id']]
            other=User(username='other',password_hash='unused',nostr_npub=npub_from_seckey((b'\x44'*32).hex()))
            world.db.add(other);world.db.commit()
            world.app.dependency_overrides[get_current_user]=lambda:other
            assert (await c.get('/api/pleroma/private-events')).json()['events']==[]
            world.app.dependency_overrides[get_current_user]=lambda:world.user
            delete=event(5,'',[['e',ev['id']]])
            assert (await world.send(delete))['ok']
            after=(await c.get('/api/pleroma/private-events')).json()['events']
            assert [e['id'] for e in after]==[delete['id']]
    asyncio.run(go())


def test_one_private_event_can_be_looked_up_by_id(world):
    """A fedi-only post is on NO relay, so a thread opened cold cannot fetch its parent.

    When somebody replies on the fediverse the bridge mirrors that reply back with an `e` tag
    pointing at the private parent (see test_fedi_bridge_replies_to_a_private_post). The client's
    `fetchEvent` asks its relays and then the public pool, and every one of those answers is
    correctly "no". Paging the whole archive to answer "what is event X" is the wrong shape: a thread
    opened from a notification or a pasted link needs exactly one event, at once.

    Owner scoping is asserted again HERE rather than assumed from the paging test above, because a
    filter added to a query is exactly where a scope gets lost.
    """
    ev=event(); other_ev=event(content='not yours')
    async def go():
        assert (await world.send(ev))['ok']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app),base_url='http://app.test') as c:
            got=(await c.get('/api/pleroma/private-events?ids='+ev['id'])).json()['events']
            assert [e['id'] for e in got]==[ev['id']]
            # An id this account does not hold answers with nothing, not with somebody else's post.
            assert (await c.get('/api/pleroma/private-events?ids='+('f'*64))).json()['events']==[]
            # Still owner-scoped: another session asking for a known id gets nothing.
            other=User(username='other2',password_hash='unused',nostr_npub=npub_from_seckey((b'\x45'*32).hex()))
            world.db.add(other);world.db.commit()
            world.app.dependency_overrides[get_current_user]=lambda:other
            assert (await c.get('/api/pleroma/private-events?ids='+ev['id'])).json()['events']==[]
            world.app.dependency_overrides[get_current_user]=lambda:world.user
            # A deletion hides it from the lookup exactly as it does from the listing.
            assert (await world.send(event(5,'',[['e',ev['id']]])))['ok']
            assert (await c.get('/api/pleroma/private-events?ids='+ev['id'])).json()['events']==[]
            # An `ids` of nothing but separators must answer with nothing -- never fall through to
            # "here is the whole archive", which is the shape this parameter could easily have had.
            assert (await c.get('/api/pleroma/private-events?ids=,,,')).json()['events']==[]
    asyncio.run(go())


def test_setting_roundtrip_preserves_existing_bridge_switches(world,monkeypatch):
    from app.routers.auth import router as auth_router
    from app.services import users_store
    world.app.include_router(auth_router)
    monkeypatch.setattr(users_store,'sync_user',AsyncMock(return_value=True))
    monkeypatch.setattr(users_store,'sync_user_kv',AsyncMock(return_value=True))
    world.user.fedi_only=False
    world.user.fedi_crosspost_enabled=True
    world.user.fedi_bridge_enabled=True
    world.db.commit()
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app),base_url='http://app.test') as c:
            assert (await c.put('/api/auth/settings',json={'fedi_only':True})).status_code==200
            result=(await c.get('/api/auth/settings')).json()
            assert result['fedi_only'] is True
            assert result['fedi_crosspost_enabled'] is True and result['fedi_bridge_enabled'] is True
            assert (await c.put('/api/auth/settings',json={'fedi_only':False})).status_code==200
            assert (await c.get('/api/auth/settings')).json()['fedi_only'] is False
    asyncio.run(go())
    assert 'fedi_only' in users_store.CONFIG_FIELDS and 'pleroma_acct' in users_store.CONFIG_FIELDS
    assert users_store._record(world.user)['fedi_only'] is False


def test_private_marker_never_drives_the_public_writeback_listener(world):
    asyncio.run(wb._handle(world.db,event()))
    assert not world.calls


def test_dm_dispatch_is_unchanged(world,monkeypatch):
    handle=AsyncMock()
    monkeypatch.setattr(wb,'_handle_dm_reply',handle)
    ev=event(1059,private=False)
    asyncio.run(wb._handle(world.db,ev))
    handle.assert_awaited_once_with(world.db,ev)


def test_media_is_uploaded_as_a_fediverse_attachment(world):
    result=asyncio.run(world.send(event(content='picture https://media.test/image.png')))
    assert result['ok']
    posted=[r for r in world.calls if r.url.path=='/api/v1/statuses' and r.method=='POST']
    body=json.loads(posted[0].content)
    assert body['status']=='picture'
    assert body['media_ids']==['media-1']
    assert any(r.url.path=='/api/v1/media' for r in world.calls)


def test_emoji_reaction_and_undo_use_the_existing_bridge_implementation(world):
    parent=seed_parent(world)
    ev=event(7,'🔥',[['e',parent['id']]])
    async def go():
        assert (await world.send(ev))['ok']
        assert (await world.send(event(5,'',[['e',ev['id']]])))['ok']
    asyncio.run(go())
    requests=[r for r in world.calls if '/reactions/' in r.url.path]
    assert [r.method for r in requests]==['PUT','DELETE']


def test_explicit_public_cleanup_still_routes_to_nostr_in_private_mode(world):
    ev=event(5, '', [['e','a'*64],['k','30311']], private=False)
    assert asyncio.run(world.send(ev)) == {'route':'nostr'}
    assert not world.calls


@pytest.mark.parametrize('kind', [1311,30311])
def test_stream_publication_is_blocked_in_private_mode(world,kind):
    result=asyncio.run(world.send(event(kind)))
    assert result['route']=='fediverse' and not result['ok']
    assert not world.calls
