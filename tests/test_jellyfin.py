"""Jellyfin Quick Connect and playback contracts, against local and proxied libraries."""
import asyncio
import copy
from contextvars import ContextVar
import json
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import User
from app.routers import jellyfin as jf, media_center as native
from app.services import media_center as media

OWNER, VIEWER, STRANGER = '11' * 32, '22' * 32, '33' * 32


@pytest.fixture(params=[False, True], ids=['local', 'nas-proxy'])
def api(request, monkeypatch, tmp_path):
    proxied = request.param
    node = ContextVar('jellyfin_test_node', default='edge')
    library = {'id': 'a' * 32, 'name': 'Movies', 'owner': OWNER, 'shared_with': [VIEWER],
               'folder': str(tmp_path), 'encoder': 'cpu', 'pages': ['page:movies'], 'count': 2}
    entries = [{'id': str(i) * 32, 'name': 'Episode ' + str(i), 'folder': 'Season 2',
                'path': 'secret/movie.mp4', 'duration': 13, 'video': True,
                'tracks': [{'index':1, 'type':'audio', 'codec':'aac', 'language':'eng', 'title':'', 'default':True, 'text':False}]} for i in (1, 2)]
    catalog = {'index': {'ids': [library['id']]}, 'library:' + library['id']: library, 'page:movies': entries}
    edge = {} if proxied else catalog
    async def read(key):
        return copy.deepcopy((catalog if node.get() == 'nas' else edge).get(key))
    async def write(key, value):
        (catalog if node.get() == 'nas' else edge)[key] = copy.deepcopy(value)
    monkeypatch.setattr(media, 'read', read)
    monkeypatch.setattr(media, 'write', write)
    monkeypatch.setattr(media, 'mutation_lock', asyncio.Lock())
    monkeypatch.setattr(jf, '_account_lock', asyncio.Lock())
    for cache in (jf._quick, jf._approvals, jf._plays, jf._locators, media._sessions, media._catalog_cache):
        cache.clear()
    monkeypatch.setattr(native.settings_store, 'get', lambda key, default=None:
                        'http://nas.lan' if proxied and node.get() == 'edge' and key == 'media_center_server_url' else default)
    monkeypatch.setattr(native.lb_auth, 'shared_secret', lambda: 'jellyfin-test-peer-secret')
    engine = create_engine('sqlite:///' + str(tmp_path / 'users.db'))
    User.__table__.create(engine)
    with Session(engine) as db:
        for name, key in [('owner', OWNER), ('viewer', VIEWER), ('stranger', STRANGER)]:
            db.add(User(username=name, password_hash='not-an-app-password', nostr_npub=key,
                        is_admin=key == OWNER, can_media=True))
        db.commit()
    def database():
        with Session(engine) as db:
            yield db
    state = {'user': VIEWER}
    def signed_in():
        if state['user'] is None:
            return None
        with Session(engine) as db:
            return db.query(User).filter(User.nostr_npub == state['user']).first()
    nas = FastAPI()
    nas.include_router(native.router)
    nas.dependency_overrides[native.get_db] = database
    @nas.middleware('http')
    async def nas_node(request, call_next):
        token = node.set('nas')
        try:
            return await call_next(request)
        finally:
            node.reset(token)
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=nas))
    monkeypatch.setattr(native, '_proxy_client', upstream)
    app = FastAPI()
    app.include_router(native.router)
    app.include_router(jf.account_router)
    app.include_router(jf.router)
    app.add_middleware(jf.ClientCORS)
    app.dependency_overrides[native.media_user_optional] = signed_in
    app.dependency_overrides[native.get_db] = database
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, catalog=catalog, edge=edge, engine=engine,
                              state=state, library=library, entries=entries, proxied=proxied)
    asyncio.run(upstream.aclose())
    engine.dispose()


def connect(api):
    c = api.client
    pending = c.post('/jellyfin/QuickConnect/Initiate').json()
    approved = c.post('/api/media-center/jellyfin-account/authorize', json={'code': pending['Code']})
    assert approved.status_code == 200, approved.text
    result = c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']})
    assert result.status_code == 200, result.text
    return result.json()


def headers(login):
    return {'Authorization': 'MediaBrowser Client="Jellyfin TV", Device="TV", DeviceId="test", Version="1", Token="' + login['AccessToken'] + '"'}


def playable(api, login):
    c, h = api.client, headers(login)
    views = c.get('/jellyfin/Users/' + login['User']['Id'] + '/Views', headers=h).json()['Items']
    result = c.get('/jellyfin/Items', params={'ParentId': views[0]['Id']}, headers=h)
    assert result.status_code == 200, result.text
    item = result.json()['Items'][0]
    info = c.post('/jellyfin/Items/' + item['Id'] + '/PlaybackInfo', headers=h, json={})
    assert info.status_code == 200, info.text
    return item, info.json()


def test_quick_connect_requires_signed_approval_and_single_use_secret(api):
    c = api.client
    assert c.get('/jellyfin/QuickConnect/Enabled').json() is True
    assert c.get('/jellyfin/Users/Public').json() == []
    pending = c.post('/jellyfin/QuickConnect/Initiate').json()
    assert len(pending['Code']) == 6 and not pending['Authenticated']
    assert c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']}).status_code == 401
    api.state['user'] = None
    assert c.post('/api/media-center/jellyfin-account/authorize', json={'code': pending['Code']}).status_code == 401
    api.state['user'] = VIEWER
    assert c.post('/api/media-center/jellyfin-account/authorize', json={'code': pending['Code']}).status_code == 200
    assert c.get('/jellyfin/QuickConnect/Connect', params={'Secret': pending['Secret']}).json()['Authenticated']
    login = c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']}).json()
    assert login['User']['Name'] == 'viewer'
    assert login['User']['Policy']['IsAdministrator'] is False
    assert c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']}).status_code == 401
    assert c.post('/jellyfin/Users/AuthenticateByName', json={'Username': 'viewer', 'Pw': 'anything'}).status_code in (404, 405)
    assert login['AccessToken'] not in json.dumps(api.edge)
    jf._quick.clear()
    assert c.get('/jellyfin/Users/Me', headers=headers(login)).status_code == 200


def test_browse_scoped_ids_sorting_no_paths_or_other_users(api):
    login = connect(api)
    item, info = playable(api, login)
    assert len(item['Id']) == 32 and item['Name'] == 'Season 2 / Episode 1'
    assert 'secret' not in json.dumps(item) and 'path' not in item
    c, h = api.client, headers(login)
    other_uid = jf.digest('jellyfin-user:' + OWNER)[:32]
    assert c.get('/jellyfin/Users/' + other_uid + '/Views', headers=h).status_code == 403
    assert c.get('/jellyfin/Items', params={'UserId': other_uid}, headers=h).status_code == 403
    page = c.get('/jellyfin/Items', params={'Recursive': True, 'StartIndex': 1, 'Limit': 1}, headers=h).json()
    assert page['TotalRecordCount'] == 2 and len(page['Items']) == 1
    assert page['Items'][0]['Name'].endswith('2')
    api.catalog['library:' + api.library['id']]['shared_with'] = []
    assert c.get('/jellyfin/Items/' + item['Id'], headers=h).status_code == 404
    assert c.get('/jellyfin/UserViews', headers=h).json()['Items'] == []


def test_hls_ticket_translation_client_bandwidth_and_stop(api, monkeypatch):
    c = api.client
    login = connect(api)
    item, info = playable(api, login)
    source = info['MediaSources'][0]
    assert not source['SupportsDirectPlay'] and not source['SupportsDirectStream']
    url = '/jellyfin/' + source['TranscodingUrl']
    master = c.get(url)
    assert master.status_code == 200, master.text
    assert '360p' in master.text and '480p' in master.text and '720p' not in master.text
    assert 'ticket=' not in master.text and '/api/media-center' not in master.text
    assert master.headers['cache-control'] == 'private, no-store'
    variant = urljoin(url, next(line for line in master.text.splitlines() if line and not line.startswith('#')))
    playlist = c.get(variant)
    segment = urljoin(variant, next(line for line in playlist.text.splitlines() if line and not line.startswith('#')))
    async def encoded(*args):
        return b'isolated-segment'
    monkeypatch.setattr(media, 'segment', encoded)
    assert c.get(segment).content == b'isolated-segment'
    h = headers(login)
    low = c.post('/jellyfin/Items/' + item['Id'] + '/PlaybackInfo', headers=h, json={'MaxStreamingBitrate': 650000}).json()
    low_url = '/jellyfin/' + low['MediaSources'][0]['TranscodingUrl']
    assert '480p' not in c.get(low_url).text
    assert c.get(low_url.replace('master.m3u8', '480p-0.ts')).status_code == 404
    assert c.post('/jellyfin/Items/' + item['Id'] + '/PlaybackInfo', headers=h,
                  json={'MaxStreamingBitrate': 1000}).json()['ErrorCode'] == 'NoCompatibleStream'
    assert c.post('/jellyfin/Sessions/Playing/Stopped', headers=h, json={'PlaySessionId': info['PlaySessionId']}).status_code == 204
    assert c.get(segment).status_code == 404


def test_revoking_permission_or_app_token_stops_existing_playback(api):
    c = api.client
    login = connect(api)
    item, info = playable(api, login)
    url = '/jellyfin/' + info['MediaSources'][0]['TranscodingUrl']
    with Session(api.engine) as db:
        user = db.query(User).filter(User.nostr_npub == VIEWER).first()
        user.can_media = False
        db.commit()
    assert c.get(url).status_code == 401
    assert c.get('/jellyfin/Users/Me', headers=headers(login)).status_code == 401
    with Session(api.engine) as db:
        user = db.query(User).filter(User.nostr_npub == VIEWER).first()
        user.can_media = True
        db.commit()
    assert c.delete('/api/media-center/jellyfin-account').status_code == 204
    assert c.get(url).status_code == 401


def test_quick_connect_expiry_bound_and_brute_force_limit(api, monkeypatch):
    c = api.client
    pending = c.post('/jellyfin/QuickConnect/Initiate').json()
    jf._quick[jf.digest(pending['Secret'])]['created'] -= 301
    assert c.get('/jellyfin/QuickConnect/Connect', params={'Secret': pending['Secret']}).status_code == 404
    for _ in range(10):
        assert c.post('/api/media-center/jellyfin-account/authorize', json={'code': '000000'}).status_code == 404
    assert c.post('/api/media-center/jellyfin-account/authorize', json={'code': '000000'}).status_code == 429
    for _ in range(128):
        assert c.post('/jellyfin/QuickConnect/Initiate').status_code == 200
    assert c.post('/jellyfin/QuickConnect/Initiate').status_code == 429


def test_tokens_are_media_only_and_logout_is_session_scoped(api):
    from app.auth import decode_token
    first, second = connect(api), connect(api)
    assert decode_token(first['AccessToken']) is None
    c = api.client
    assert c.post('/jellyfin/Sessions/Logout', headers=headers(first)).status_code == 204
    assert c.get('/jellyfin/Users/Me', headers=headers(first)).status_code == 401
    assert c.get('/jellyfin/Users/Me', headers=headers(second)).status_code == 200
    assert c.get('/jellyfin/System/Configuration', headers=headers(second)).status_code == 404


def test_official_sdk_quick_connect_and_real_hls_decode(api, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    import shutil
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    sdk = os.environ.get('JELLYFIN_TEST_SDK')
    if not sdk or not Path(sdk, 'lib/index.js').exists():
        pytest.skip('Set JELLYFIN_TEST_SDK to an installed @jellyfin/sdk package')
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg required for client integration')
    source = tmp_path / 'movie.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=24',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '13', '-c:v', 'libx264',
                    '-threads', '1', '-c:a', 'aac', str(source)], check=True, timeout=20)
    monkeypatch.setenv('POSTERCHANAI_MEDIA_ROOTS', str(tmp_path))
    monkeypatch.setenv('POSTERCHANAI_MEDIA_CACHE', str(tmp_path / 'cache'))
    from PIL import Image
    Image.new('RGB', (600, 400), '#235678').save(tmp_path / 'poster.jpg')
    api.catalog['page:movies'] = media.scan(str(tmp_path))[0]
    monkeypatch.setattr(media, '_job_condition', asyncio.Condition())
    monkeypatch.setattr(media, '_rate_lock', asyncio.Lock())
    media._segment_jobs.clear()
    media._rate_due.clear()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.client.app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        result = subprocess.run(['node', 'tests/jellyfin/sdk_quick_connect.mjs'],
                                env={**os.environ, 'JELLYFIN_TEST_SERVER': f'http://127.0.0.1:{port}/jellyfin'},
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert 'real FFmpeg decode' in result.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_web_client_cors_is_limited_to_token_api(api):
    c = api.client
    origin = 'https://app.jellyfin.org'
    preflight = c.options('/jellyfin/QuickConnect/Initiate', headers={
        'Origin': origin, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'Authorization,Content-Type'})
    assert preflight.status_code == 200
    assert preflight.headers['access-control-allow-origin'] == '*'
    assert 'access-control-allow-credentials' not in preflight.headers
    assert 'access-control-allow-origin' not in c.options('/api/media-center/jellyfin-account/authorize',
        headers={'Origin': origin, 'Access-Control-Request-Method': 'POST'}).headers
    assert c.get('/jellyfin/Items', headers={'Origin': origin}).status_code == 401


def test_disconnect_cancels_pending_approvals_and_tokens_expire(api):
    c = api.client
    login = connect(api)
    uid = login['User']['Id']
    api.edge[jf.account_key(uid)]['sessions'][0]['expires'] = 0
    assert c.get('/jellyfin/Users/Me', headers=headers(login)).status_code == 401
    pending = c.post('/jellyfin/QuickConnect/Initiate').json()
    c.post('/api/media-center/jellyfin-account/authorize', json={'code': pending['Code']})
    c.delete('/api/media-center/jellyfin-account')
    assert c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']}).status_code == 401


def test_quick_connect_token_is_not_issued_until_relay_ack(api, monkeypatch):
    from fastapi import HTTPException
    c = api.client
    pending = c.post('/jellyfin/QuickConnect/Initiate').json()
    c.post('/api/media-center/jellyfin-account/authorize', json={'code': pending['Code']})
    saved_write = media.write
    async def failed_write(*args):
        raise HTTPException(503, 'Test relay unavailable')
    monkeypatch.setattr(media, 'write', failed_write)
    assert c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']}).status_code == 503
    assert jf.digest(pending['Secret']) in jf._quick
    monkeypatch.setattr(media, 'write', saved_write)
    assert c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'Secret': pending['Secret']}).status_code == 200


def test_quick_connect_accepts_only_ascii_six_digit_codes(api):
    login = connect(api)
    assert api.client.post('/api/media-center/jellyfin-account/authorize', json={'code': '١٢٣٤٥٦'}).status_code == 422
    assert api.client.post('/jellyfin/QuickConnect/Authorize', params={'Code': '١٢٣٤٥٦'}, headers=headers(login)).status_code == 400


def test_jellyfin_credentials_never_federate():
    from app.services.nostr_relay.server import _broadcastable, _private_mirrorable
    event = {'kind': 30078, 'tags': [['d', media.NS + jf.account_key('a' * 32)]]}
    assert not _broadcastable(event, {'backup_datastore': True})
    assert not _private_mirrorable(event)


def test_client_websocket_keepalive_and_revocation(api):
    from starlette.websockets import WebSocketDisconnect
    login = connect(api)
    with api.client.websocket_connect('/jellyfin/socket?api_key=' + login['AccessToken']) as ws:
        assert ws.receive_json()['MessageType'] == 'ForceKeepAlive'
        ws.send_json({'MessageType': 'KeepAlive'})
        assert ws.receive_json()['MessageType'] == 'KeepAlive'
        api.client.post('/jellyfin/Sessions/Logout', headers=headers(login))
        ws.send_json({'MessageType': 'KeepAlive'})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008
    assert jf._socket_count == 0


def test_api_does_not_serve_jellyfin_web_ui(api):
    assert api.client.get('/jellyfin/web/index.html').status_code == 404


def test_playback_info_audio_and_subtitle_contract(api):
    for entry in api.entries:
        entry['tracks'] += [
            {'index':2,'type':'audio','codec':'eac3','language':'jpn','title':'Japanese','default':False,'text':False},
            {'index':3,'type':'subtitle','codec':'ass','language':'eng','title':'English','default':False,'text':True},
            {'index':4,'type':'subtitle','codec':'hdmv_pgs_subtitle','language':'jpn','title':'Japanese','default':False,'text':False}]
    api.catalog['page:movies'] = api.entries
    login = connect(api)
    item, _ = playable(api, login)
    uid = item['Id']
    response = api.client.post('/jellyfin/Items/'+uid+'/PlaybackInfo', headers={'X-Emby-Token':login['AccessToken']},
                               json={'AudioStreamIndex':2,'SubtitleStreamIndex':3})
    assert response.status_code == 200
    info=response.json();source=info['MediaSources'][0]
    assert source['DefaultAudioStreamIndex']==2 and source['DefaultSubtitleStreamIndex']==3
    text=next(s for s in source['MediaStreams'] if s['Index']==3)
    assert text['IsTextSubtitleStream'] and text['DeliveryMethod']=='External'
    assert '/Subtitles/3/Stream.vtt?' in text['DeliveryUrl']
    bitmap=next(s for s in source['MediaStreams'] if s['Index']==4)
    assert not bitmap['IsTextSubtitleStream'] and bitmap['DeliveryMethod']=='Encode'
    record=jf._plays[info['PlaySessionId']]
    assert 'audio=2' in record['url'] and 'subtitle=-1' in record['url']
    master = api.client.get('/jellyfin/'+source['TranscodingUrl']+'&AudioStreamIndex=1&SubtitleStreamIndex=4')
    assert master.status_code == 200
    assert 'AudioStreamIndex=1' in master.text and 'SubtitleStreamIndex=4' in master.text
    assert api.client.get('/jellyfin/'+source['TranscodingUrl']+'&AudioStreamIndex=999').status_code == 400
    bad=api.client.post('/jellyfin/Items/'+uid+'/PlaybackInfo',headers={'X-Emby-Token':login['AccessToken']},json={'AudioStreamIndex':3})
    assert bad.status_code==400
