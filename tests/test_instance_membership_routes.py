"""Actual scoped HTTP routes keep their auth and enforce signed instance membership."""
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.routers import news, mail, git, torrent, office, media_center, websearch, monero_user_wallet
from app.services import instance_membership as membership
from app.services.nostr.event import build_event

SECRET = bytes.fromhex('01'.zfill(64))
SIGNED = build_event(SECRET, 0, json.dumps({'nip05':'alice@example.test'}), created_at=100)
PK = SIGNED['pubkey']


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture
def setup(monkeypatch):
    state = {'rows':[SIGNED], 'clock':0}
    async def query(pk, port): return state['rows']
    checker = membership.MembershipChecker(query=query, configuration=lambda:(f'alice {PK}','example.test','','3052'), clock=lambda:state['clock'])
    monkeypatch.setattr(membership, '_checker', checker)
    user = SimpleNamespace(id=7, nostr_npub=PK, is_admin=False, can_media=True, can_torrent=True, news_sources='')
    app = FastAPI()
    for module in [news, mail, git, torrent, office, media_center, websearch]:
        app.include_router(module.router)
        if hasattr(module,'get_current_user'):
            app.dependency_overrides[module.get_current_user] = lambda:user
        if hasattr(module,'get_db'):
            app.dependency_overrides[module.get_db] = lambda:None
    app.dependency_overrides[media_center.media_user_optional] = lambda:user
    monkeypatch.setattr(torrent, 'get_current_user', lambda *args:user)
    monkeypatch.setattr(torrent.lb_auth, 'is_internal', lambda request:False)
    monkeypatch.setattr(mail, 'get_user_mail_accounts', lambda *args:[])
    async def empty(*args): return []
    monkeypatch.setattr(torrent, 'scrape_torrents', empty)
    monkeypatch.setattr(git, '_enabled', lambda:True)
    monkeypatch.setattr(git.git_proxy, 'proxy_enabled', lambda:True)
    monkeypatch.setattr(media_center.settings_store, 'get', lambda key, default=None:default)
    monkeypatch.setattr(media_center.media, 'libraries', empty)
    class SearchStub:
        async def search_page(self, *args, **kwargs): return {'results':[]}
    monkeypatch.setattr(websearch, 'get_search_service', lambda db:SearchStub())
    app.include_router(monero_user_wallet.router, prefix='/api/test-user-wallet')
    app.dependency_overrides[monero_user_wallet.auth.get_current_user] = lambda:user
    async def balance(pk):
        assert pk == PK
        return {'balance':'12'}
    monkeypatch.setattr(monero_user_wallet.user_wallets, 'balance', balance)
    async def limits(): return {'viewer_kbps':10000}
    monkeypatch.setattr(media_center.media, 'limits', limits)
    return app, state, user


@pytest.mark.anyio
@pytest.mark.parametrize('path',['/api/news/sources','/api/mail/accounts','/api/torrent/catalog','/api/git/status','/client/office/blank/text','/api/media-center','/api/websearch/search?q=hello','/api/test-user-wallet/balance'])
async def test_route_allows_matching_profile_and_rejects_latest_removed_address(setup,path):
    app,state,user=setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get(path)
        assert response.status_code==200,response.text
        state['rows']=[build_event(SECRET,0,'{}',created_at=101)]
        state['clock']=31
        response=await client.get(path)
        assert response.status_code==403,response.text


@pytest.mark.anyio
async def test_office_capability_binds_owner_and_rechecks_membership(setup,monkeypatch,tmp_path):
    app,state,user=setup
    monkeypatch.setattr(office,'_ROOT',tmp_path)
    ident='a'*32
    folder=tmp_path/ident;folder.mkdir()
    (folder/'document').write_bytes(b'document')
    (folder/'meta.json').write_text(json.dumps({'owner':PK,'name':'report.docx','size':8,'version':1,'readonly':False}))
    token=office._token(ident,int(time.time())+60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        # Capability requests do not need browser cookies/JWT (Collabora protocol).
        result=await client.get(f'/wopi/files/{ident}',params={'access_token':token})
        assert result.status_code==200,result.text
        assert (await client.get(f'/wopi/files/{ident}',params={'access_token':'bad'})).status_code==401
        state['rows']=[build_event(SECRET,0,'{}',created_at=101)];state['clock']=31
        assert (await client.get(f'/wopi/files/{ident}',params={'access_token':token})).status_code==403


@pytest.mark.anyio
async def test_search_ticket_and_cached_page_recheck_profile(setup,monkeypatch):
    app,state,user=setup
    app.dependency_overrides[websearch._page_viewer] = lambda:user
    # A denied ticket/page must not reach fetch work at all.
    state['rows']=[build_event(SECRET,0,'{}',created_at=101)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/websearch/ticket')).status_code==403
        assert (await client.get('/api/websearch/page',params={'url':'https://example.test'})).status_code==403
        assert (await client.get('/api/websearch/asset',params={'url':'https://example.test/style.css'})).status_code==403


@pytest.mark.anyio
async def test_signed_sync_and_meme_requests_require_profile_and_valid_ownership(setup, monkeypatch):
    import base64
    from app.routers import client as client_router
    from app.services import blossom_service
    app,state,user=setup
    app.include_router(client_router.router)
    class EmptyDB:
        def query(self,*args): return self
        def filter(self,*args): return self
        def first(self): return None
    app.dependency_overrides[client_router.get_db]=lambda:EmptyDB()
    reached=[]
    def disabled(db):
        reached.append(True)
        return False
    monkeypatch.setattr(blossom_service, 'is_enabled', disabled)
    proof=base64.b64encode(json.dumps(build_event(SECRET,27235,'',created_at=int(time.time()))).encode()).decode()
    body={'pubkey':PK,'auth':proof}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        r=await client.post('/client/sync-folders',json=body)
        assert r.status_code==200 and r.json()['folders']==[]
        r=await client.post('/client/meme/generate-image',json={**body,'prompt':'test'})
        assert r.status_code==503 and 'storage' in r.json()['detail']
        assert reached==[True]
        state['rows']=[build_event(SECRET,0,'{}',created_at=101)];state['clock']=31
        assert (await client.post('/client/sync-folders',json=body)).status_code==403
        assert (await client.post('/client/meme/generate-image',json={**body,'prompt':'test'})).status_code==403
        assert reached==[True]
        assert (await client.post('/client/meme/generate-image',json={**body,'auth':'bad','prompt':'test'})).status_code==401


@pytest.mark.anyio
async def test_raw_search_api_rejects_anonymous_and_nonmembers_but_keeps_trusted_peers(setup,monkeypatch):
    from app.routers import search_api
    from app.services import search_service
    app,state,user=setup
    app.include_router(search_api.router)
    caller={'user':user,'peer':False}
    monkeypatch.setattr(search_api, 'get_current_user_optional', lambda request,db:caller['user'])
    monkeypatch.setattr(search_api.lb_auth, 'is_internal', lambda request:caller['peer'])
    class Search:
        async def web_search_local(self,*args,**kwargs): return []
    monkeypatch.setattr(search_service,'get_search_service',lambda db:Search())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/search',json={'query':'test'})).status_code==200
        state['rows']=[build_event(SECRET,0,'{}',created_at=101)];state['clock']=31
        assert (await client.post('/api/search',json={'query':'test'})).status_code==403
        caller['user']=None
        assert (await client.post('/api/search',json={'query':'test'})).status_code==401
        caller['peer']=True
        assert (await client.post('/api/search',json={'query':'test'})).status_code==200
