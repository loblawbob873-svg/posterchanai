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
    monkeypatch.setattr(media, '_progress_lock', asyncio.Lock())
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
    while item['IsFolder']:
        item = c.get('/jellyfin/Items', params={'ParentId': item['Id']}, headers=h).json()['Items'][0]
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
    assert len(item['Id']) == 32 and item['Name'] == 'Episode 1'
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
    # Kotlin UrlBuilder appends both slash-prefixed and relative paths to /jellyfin.
    assert ('/jellyfin/' + text['DeliveryUrl'].lstrip('/')).count('/jellyfin/') == 1
    assert text['DeliveryUrl'].startswith('Videos/')
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


def test_server_url_and_case_insensitive_discovery(api):
    c = api.client
    expected = c.get('/jellyfin/System/Info/Public').json()
    for path in ('/jellyfin', '/jellyfin/', '/Jellyfin/system/info/public', '/jellyfin/system/info/public'):
        response = c.get(path, follow_redirects=False)
        assert response.status_code == 200
        assert response.json()['Id'] == expected['Id']
        assert response.json()['ProductName'] == 'Jellyfin Server'
        assert 'location' not in response.headers
    assert c.get('/jellyfin/quickconnect/enabled').json() is True
    assert c.head('/jellyfin').status_code == 200
    assert c.get('/jellyfin', headers={'X-Forwarded-Proto':'https'}).json()['LocalAddress'] == 'https://testserver/jellyfin'
    pending = c.post('/jellyfin/quickconnect/initiate').json()
    state = c.get('/jellyfin/quickconnect/connect', params={'Secret':pending['Secret']})
    assert state.status_code == 200 and state.json()['Authenticated'] is False
    assert c.get('/jellyfin/users/me').status_code == 401
    assert c.get('/jellyfin/web/index.html').status_code == 404
    login = connect(api)
    assert c.get('/jellyfin/users/me', headers=headers(login)).json()['Id'] == login['User']['Id']


def test_tv_camel_case_quick_connect_and_playback_requests(api):
    c=api.client
    pending=c.post('/jellyfin/QuickConnect/Initiate').json()
    assert c.post('/api/media-center/jellyfin-account/authorize',json={'code':pending['Code']}).status_code==200
    assert c.get('/jellyfin/QuickConnect/Connect',params={'secret':pending['Secret']}).json()['Authenticated']
    result=c.post('/jellyfin/Users/AuthenticateWithQuickConnect',json={'secret':pending['Secret']})
    assert result.status_code==200, result.text
    login=result.json()
    item, _=playable(api,login)
    info=c.post('/jellyfin/Items/'+item['Id']+'/PlaybackInfo',headers=headers(login),
                json={'audioStreamIndex':None,'subtitleStreamIndex':None,'maxStreamingBitrate':700000})
    assert info.status_code==200, info.text
    play_id=info.json()['PlaySessionId']
    assert jf._plays[play_id]['profiles']==['360p']
    assert c.post('/jellyfin/Sessions/Playing',headers=headers(login),json={'playSessionId':play_id}).status_code==204
    assert c.post('/jellyfin/Sessions/Playing/Stopped',headers=headers(login),json={'playSessionId':play_id}).status_code==204
    assert c.post('/jellyfin/Users/AuthenticateWithQuickConnect',json={'secret':pending['Secret']}).status_code==401


def test_kotlin_tv_startup_and_playback_contract(api):
    """Kotlin deserialization requires fields that the JavaScript SDK tolerates omitting."""
    from pathlib import Path
    from uuid import UUID
    from datetime import datetime
    contract = json.loads((Path(__file__).parent / 'jellyfin/kotlin_contract.json').read_text())['models']

    def validate(value, kind, path='$'):
        if kind.endswith('?'):
            if value is None:
                return
            kind = kind[:-1]
        if kind.startswith('List<'):
            assert isinstance(value, list), path
            for i, entry in enumerate(value):
                validate(entry, kind[5:-1], f'{path}[{i}]')
        elif kind.startswith('Map<'):
            assert isinstance(value, dict), path
        elif kind == 'UUID':
            UUID(value)
        elif kind == 'DateTime':
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif kind in ('Boolean', 'String', 'Int', 'Long', 'Float', 'Double'):
            expected = {'Boolean': bool, 'String': str, 'Int': int, 'Long': int,
                        'Float': (float, int), 'Double': (float, int)}[kind]
            assert isinstance(value, expected), (path, kind)
        elif kind in contract:
            schema = contract[kind]
            if 'enum' in schema:
                assert value in schema['enum'], (path, value)
            else:
                assert isinstance(value, dict), path
                for key, field in schema['fields'].items():
                    assert not field['required'] or key in value, f'{path}.{key} required by Kotlin SDK'
                    if key in value:
                        validate(value[key], field['type'], f'{path}.{key}')
        else:
            raise AssertionError(f'Unhandled SDK type: {kind}')

    login = connect(api)
    validate(login, 'AuthenticationResult')
    c, h = api.client, headers(login)
    for path, kind in [('/System/Info/Public', 'PublicSystemInfo'),
                       ('/Users/Me', 'UserDto'),
                       ('/DisplayPreferences/usersettings?client=android', 'DisplayPreferencesDto'),
                       ('/system/configuration/encoding', 'EncodingOptions'),
                       ('/userviews', 'BaseItemDtoQueryResult')]:
        response = c.get('/jellyfin' + path, headers=h)
        assert response.status_code == 200, response.text
        validate(response.json(), kind)
    for session in c.get('/jellyfin/Sessions', headers=h).json():
        validate(session, 'SessionInfoDto')
    item, playback = playable(api, login)
    validate(item, 'BaseItemDto')
    validate(playback, 'PlaybackInfoResponse')
    for image in c.get('/jellyfin/Items/'+item['Id']+'/Images', headers=h).json():
        validate(image, 'ImageInfo')
    assert c.get('/jellyfin/System/Configuration/encoding').status_code == 401
    assert c.get('/jellyfin/System/Configuration/network', headers=h).status_code == 404
    assert c.post('/jellyfin/System/Configuration/encoding', headers=h, json={}).status_code == 405


def test_android_phone_host_negotiates_html_without_changing_api_discovery(api):
    c = api.client
    for path in ['/jellyfin', '/jellyfin/']:
        response = c.get(path, headers={'Accept': 'text/html,application/xhtml+xml'})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/html')
        assert 'main.posterchan.bundle.js' in response.text
        assert 'no-store' in response.headers['cache-control']
        assert response.headers['vary'] == 'Accept'
        assert c.get(path).json()['ProductName'] == 'Jellyfin Server'
    # The native discovery request stays JSON even if a client sends a browser Accept header.
    assert c.get('/jellyfin/System/Info/Public', headers={'Accept': 'text/html'}).json()['Id']
    script = c.get('/jellyfin/main.posterchan.bundle.js?deferred=true&ts=123')
    assert script.status_code == 200
    assert script.headers['content-type'].startswith('application/javascript')
    assert 'jellyfin_credentials' in script.text
    assert c.get('/jellyfin/Users/Me').status_code == 401


def test_android_host_browser_quick_connect_browse_play_and_reconnect(api, monkeypatch, tmp_path):
    """Real phone-width browser, native-style deferred script handoff, API and FFmpeg HLS."""
    import os
    from pathlib import Path
    import shutil
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    import websockets
    from fastapi.staticfiles import StaticFiles
    from scripts.check_media_center import Browser
    chrome = shutil.which('google-chrome') or shutil.which('chromium') or '/opt/google/chrome/chrome'
    if not Path(chrome).exists() or not shutil.which('ffmpeg'):
        pytest.skip('Chrome and FFmpeg required for Android host browser test')
    source = tmp_path / 'Anime' / 'Season 2' / 'movie.mkv'
    source.parent.mkdir(parents=True)
    captions = tmp_path / 'captions.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:30,000\nCaption test visible\n')
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=24',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-f', 'lavfi', '-i', 'sine=frequency=880',
                    '-i', str(captions), '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s',
                    '-t', '30', '-c:v', 'libx264', '-threads', '1', '-c:a', 'aac', '-c:s', 'srt',
                    '-metadata:s:a:0', 'language=eng', '-metadata:s:a:1', 'language=jpn',
                    '-metadata:s:s:0', 'language=eng', str(source)], check=True, timeout=20)
    monkeypatch.setenv('POSTERCHANAI_MEDIA_ROOTS', str(tmp_path))
    monkeypatch.setenv('POSTERCHANAI_MEDIA_CACHE', str(tmp_path / 'cache'))
    api.catalog['page:movies'] = media.scan(str(tmp_path))[0]
    monkeypatch.setattr(media, '_job_condition', asyncio.Condition())
    monkeypatch.setattr(media, '_rate_lock', asyncio.Lock())
    media._segment_jobs.clear()
    media._rate_due.clear()
    root = Path(__file__).resolve().parents[1]
    api.client.app.mount('/static', StaticFiles(directory=root / 'static'))
    observed = []
    browser_app = FastAPI()
    browser_app.mount('/', api.client.app)
    @browser_app.middleware('http')
    async def android_handoff(request, call_next):
        if request.url.path.endswith('/main.posterchan.bundle.js'):
            observed.append('deferred' in request.query_params)
            if 'deferred' not in request.query_params:
                # Android intercepts this request, signals readiness and reloads it deferred.
                return jf.Response("const s=document.createElement('script');s.src=document.currentScript.src+'?deferred=true';document.body.append(s);", media_type='application/javascript')
        return await call_next(request)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(browser_app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    profile = tmp_path / 'chrome'
    process = subprocess.Popen([chrome, '--headless=new', '--no-sandbox', '--disable-gpu',
                                '--autoplay-policy=no-user-gesture-required', '--remote-debugging-port=0',
                                '--user-data-dir='+str(profile), 'about:blank'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    async def exercise():
        # This client also downloads bandwidth-paced HLS alongside the browser.
        async with httpx.AsyncClient(timeout=60) as client:
            for _ in range(100):
                if server.started and (profile / 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            debug_port = (profile / 'DevToolsActivePort').read_text().splitlines()[0]
            pages = (await client.get('http://127.0.0.1:'+debug_port+'/json/list')).json()
            page = next(p for p in pages if p['type'] == 'page')
            async with websockets.connect(page['webSocketDebuggerUrl'], max_size=32*1024*1024) as ws:
                browser = Browser(ws)
                await browser.call('Emulation.setDeviceMetricsOverride', {'width':390, 'height':844, 'deviceScaleFactor':1, 'mobile':False})
                await browser.call('Page.navigate', {'url':f'http://127.0.0.1:{port}/jellyfin/'})
                await browser.until("/^\\d{6}$/.test(document.querySelector('#code')?.textContent)")
                code = await browser.js("document.querySelector('#code').textContent")
                result = await client.post(f'http://127.0.0.1:{port}/api/media-center/jellyfin-account/authorize', json={'code':code})
                assert result.status_code == 200
                await browser.until("!!document.querySelector('.card')")
                assert observed[:2] == [False, True]
                assert await browser.js("!!JSON.parse(localStorage.jellyfin_credentials).Servers[0].AccessToken")
                assert await browser.js('document.documentElement.scrollWidth<=innerWidth')
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('#heading').textContent==='Movies' && document.querySelector('.card')?.textContent.includes('Anime')")
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('#heading').textContent==='Anime' && document.querySelector('.card')?.textContent.includes('Season 2')")
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('#heading').textContent==='Season 2' && document.querySelector('.card')?.textContent==='movie'")
                await browser.js("document.querySelector('#back').click()", True)
                await browser.until("document.querySelector('#heading').textContent==='Anime' && document.querySelector('.card')?.textContent.includes('Season 2')")
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('#heading').textContent==='Season 2' && document.querySelector('.card')?.textContent==='movie'")
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('video').currentTime>1")
                await browser.js("document.querySelector('#subtitles').value='3';document.querySelector('#subtitles').dispatchEvent(new Event('change'))", True)
                await browser.until("document.querySelector('video').textTracks[0]?.mode==='showing' && document.querySelector('video').textTracks[0]?.activeCues?.[0]?.text==='Caption test visible'")
                await browser.js("document.querySelector('#subtitles').value='-1';document.querySelector('#subtitles').dispatchEvent(new Event('change'))", True)
                assert await browser.js("!document.querySelector('video track')")
                await browser.js("document.querySelector('#audio').value='2';document.querySelector('#audio').dispatchEvent(new Event('change'))", True)
                await browser.until("document.querySelector('#audio').value==='2' && document.querySelector('video').currentTime>1 && !document.querySelector('video').paused")
                assert any('audio=2' in record['url'] for record in jf._plays.values())
                # Decode the actual selected stream: English is 440 Hz, Japanese 880 Hz.
                from array import array
                from urllib.parse import urlencode
                play_id, record = next((key, value) for key, value in jf._plays.items() if 'audio=2' in value['url'])
                token = await browser.js("JSON.parse(localStorage.pc_media_app).AccessToken")
                segment = await client.get(f"http://127.0.0.1:{port}/jellyfin/Videos/{record['item']}/360p-0.ts?" + urlencode({'api_key':token,'PlaySessionId':play_id}))
                assert segment.status_code == 200
                pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-t', '2', '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1'], input=segment.content, capture_output=True, check=True, timeout=15).stdout
                samples = array('h', pcm)
                frequency = sum(a <= 0 < b for a,b in zip(samples,samples[1:])) * 8000 / len(samples)
                assert 850 < frequency < 910, frequency

                await browser.js("document.querySelector('#subtitles').value='3';document.querySelector('#subtitles').dispatchEvent(new Event('change'))", True)
                await browser.until("document.querySelector('video').textTracks[0]?.mode==='showing' && document.querySelector('video').textTracks[0]?.activeCues?.[0]?.text==='Caption test visible'")

                assert await browser.js('document.documentElement.scrollWidth<=innerWidth')
                # The Android bridge is required: requestFullscreen alone does not
                # expand a WebView without a native custom-view implementation.
                await browser.js("window.fullscreenCalls=[];window.NativeInterface={enableFullscreen(){fullscreenCalls.push('enter');},disableFullscreen(){fullscreenCalls.push('exit');}}")
                await browser.js("document.querySelector('#fullscreen').click()", True)
                assert await browser.js("document.body.classList.contains('mc-fullscreen') && fullscreenCalls.at(-1)==='enter'")
                assert await browser.js("Math.abs(document.querySelector('#player').getBoundingClientRect().height-innerHeight)<2")
                # Verify the video itself fills the viewport, not just its wrapper.
                for width, height in [(390,844),(844,390)]:
                    await browser.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':height,'deviceScaleFactor':1,'mobile':True})
                    assert await browser.js("(()=>{const r=document.querySelector('video').getBoundingClientRect();return Math.abs(r.width-innerWidth)<2 && Math.abs(r.height-innerHeight)<2 && Math.abs(r.top)<2 && Math.abs(r.left)<2;})()")
                await browser.until("document.querySelector('#player').classList.contains('mc-controls-hidden')")
                await browser.js("document.querySelector('video').dispatchEvent(new Event('pointerdown',{bubbles:true}))")
                assert await browser.js("!document.querySelector('#player').classList.contains('mc-controls-hidden')")
                await browser.js("history.back()")
                await browser.until("!document.body.classList.contains('mc-fullscreen') && fullscreenCalls.at(-1)==='exit'")
                await browser.js("delete window.NativeInterface")
                await browser.js("document.querySelector('#fullscreen').click()", True)
                await browser.until("document.fullscreenElement===document.querySelector('#player')")
                await browser.js("document.querySelector('#fullscreen').click()", True)
                await browser.until("!document.fullscreenElement && !document.body.classList.contains('mc-fullscreen')")
                await browser.js("document.querySelector('#stop').click()", True)
                await browser.until("document.querySelector('#player').hidden")
                await browser.call('Page.reload')
                await browser.until("!!document.querySelector('.card') && document.querySelector('#connect').hidden")
                for label in ['Movies', 'Anime', 'Season 2']:
                    await browser.js("document.querySelector('.card').click()", True)
                    await browser.until("document.querySelector('#heading').textContent==="+json.dumps(label)+" && !!document.querySelector('.card')")
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('#resume-dialog').open")
                assert await browser.js("document.querySelector('#resume-time').textContent!=='00:00'")
                await browser.js("document.querySelector('#resume-dialog button[value=resume]').click()", True)
                await browser.until("document.querySelector('video').currentTime>1 && !document.querySelector('video').paused")
                await browser.js("document.querySelector('#stop').click()", True)
                await browser.until("document.querySelector('#player').hidden")
                await browser.js("document.querySelector('.card').click()", True)
                await browser.until("document.querySelector('#resume-dialog').open")
                await browser.js("document.querySelector('#resume-dialog button[value=start]').click()", True)
                await browser.until("!document.querySelector('#player').hidden && document.querySelector('video').readyState>=2")
                assert await browser.js("document.querySelector('video').currentTime<3")

                await browser.js("document.querySelector('#logout').click()", True)
                await browser.until("/^\\d{6}$/.test(document.querySelector('#code')?.textContent) && !localStorage.getItem('pc_media_app')")
    try:
        asyncio.run(exercise())
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_android_tv_accepts_file_source_and_can_release_transcode(api):
    first, second = connect(api), connect(api)
    item, info = playable(api, first)
    source = info['MediaSources'][0]
    # JellyfinMediaStreamResolver filters these fields before checking transcode support.
    assert source['Protocol'] == 'File' and source['IsRemote'] is False
    assert source['SupportsTranscoding'] and not source['SupportsDirectPlay']
    assert 'Path' not in source  # The device never receives NAS filesystem paths.
    url = '/jellyfin/Videos/ActiveEncodings?playSessionId=' + info['PlaySessionId']
    assert api.client.delete(url).status_code == 401
    assert api.client.delete(url, headers=headers(second)).status_code == 204
    assert info['PlaySessionId'] in jf._plays
    assert api.client.delete(url, headers=headers(first)).status_code == 204
    assert info['PlaySessionId'] not in jf._plays
    assert api.client.delete(url, headers=headers(first)).status_code == 204


def test_folder_hierarchy_artwork_search_and_acl(api, tmp_path, monkeypatch):
    monkeypatch.setenv('POSTERCHANAI_MEDIA_ROOTS', str(tmp_path))
    from PIL import Image
    entries = api.catalog['page:movies']
    entries[0]['folder'] = 'Anime/Season 2'
    entries[1]['folder'] = 'Anime/Season 10'
    directory = tmp_path / 'Anime'
    directory.mkdir()
    Image.new('RGB', (400, 250), '#274975').save(directory / 'folder.png')
    login = connect(api)
    c, h = api.client, headers(login)
    lib = c.get('/jellyfin/UserViews', headers=h).json()['Items'][0]
    rows = c.get('/jellyfin/items/', params={'ParentId':lib['Id']}, headers=h).json()['Items']
    assert [(row['Name'], row['Type']) for row in rows] == [('Anime', 'Folder')]
    anime = rows[0]
    children = c.get('/jellyfin/Items', params={'ParentId':anime['Id']}, headers=h).json()['Items']
    assert [row['Name'] for row in children] == ['Season 2', 'Season 10']
    assert all(row['ParentId'] == anime['Id'] for row in children)
    row = c.get('/jellyfin/Items', params={'ParentId':children[0]['Id']}, headers=h).json()['Items'][0]
    assert row['Name'] == 'Episode 1' and row['ParentId'] == children[0]['Id']
    jf._locators.clear()  # Folder identities survive cache eviction/restart.
    assert c.get('/jellyfin/Items/'+anime['Id'], headers=h).json()['Name'] == 'Anime'
    artwork = c.get('/jellyfin/Items/'+anime['Id']+'/Images/Primary', headers=h)
    assert artwork.status_code == 200 and artwork.headers['content-type'].startswith('image/')
    assert c.post('/jellyfin/Items/'+anime['Id']+'/PlaybackInfo', json={}, headers=h).status_code == 400
    search = c.get('/jellyfin/Items', params={'ParentId':anime['Id'], 'SearchTerm':'Season 10'}, headers=h).json()['Items']
    assert [row['Name'] for row in search] == ['Episode 2']
    assert c.get('/jellyfin/useritems/resume', headers=h).json()['Items'] == []
    api.catalog['library:'+api.library['id']]['shared_with'] = []
    assert c.get('/jellyfin/Items/'+anime['Id'], headers=h).status_code == 404
    assert c.get('/jellyfin/Items/'+anime['Id']+'/Images/Primary', headers=h).status_code == 404


def test_progress_persists_per_user_and_resume_can_restart(api):
    login = connect(api)
    item, info = playable(api, login)
    c, h = api.client, headers(login)
    body = {'playSessionId':info['PlaySessionId'], 'positionTicks':60000000}
    assert c.post('/jellyfin/Sessions/Playing/Progress', headers=h, json=body).status_code == 204
    assert c.get('/jellyfin/Items/'+item['Id'], headers=h).json()['UserData']['PlaybackPositionTicks'] == 60000000
    assert c.get('/jellyfin/UserItems/Resume', headers=h).json()['Items'][0]['Id'] == item['Id']
    # A different app token belonging to this person hydrates the same saved position.
    second = connect(api)
    jf._plays.clear()
    jf._locators.clear()
    assert c.get('/jellyfin/Items/'+item['Id'], headers=headers(second)).json()['UserData']['PlaybackPositionTicks'] == 60000000
    api.state['user'] = OWNER
    owner = connect(api)
    assert c.get('/jellyfin/Items/'+item['Id'], headers=headers(owner)).json()['UserData']['PlaybackPositionTicks'] == 0
    assert c.get('/jellyfin/UserItems/Resume', headers=headers(owner)).json()['Items'] == []
    _, info = playable(api, second)
    body = {'PlaySessionId':info['PlaySessionId'], 'PositionTicks':0}
    assert c.post('/jellyfin/Sessions/Playing', headers=headers(second), json=body).status_code == 204
    assert c.get('/jellyfin/UserItems/Resume', headers=headers(second)).json()['Items'] == []
    body['PositionTicks'] = 130000000  # Completion clears Resume but retains Played.
    assert c.post('/jellyfin/Sessions/Playing/Stopped', headers=headers(second), json=body).status_code == 204
    saved = c.get('/jellyfin/Items/'+item['Id'], headers=headers(second)).json()['UserData']
    assert saved['Played'] and saved['PlaybackPositionTicks'] == 0
    # Revocation applies to reading/writing progress as well as streaming.
    api.catalog['library:'+api.library['id']]['shared_with'] = []
    assert c.get('/jellyfin/Items/'+item['Id'], headers=headers(second)).status_code == 404
    assert c.get('/jellyfin/UserItems/Resume', headers=headers(second)).json()['Items'] == []


def test_media_permission_denied_by_default_and_revokes_app_access(api):
    with Session(api.engine) as db:
        user = db.query(User).filter(User.nostr_npub == VIEWER).first()
        user.can_media = False
        db.commit()
    pending = api.client.post('/jellyfin/QuickConnect/Initiate').json()
    assert api.client.post('/api/media-center/jellyfin-account/authorize', json={'code':pending['Code']}).status_code == 403
    with Session(api.engine) as db:
        user = db.query(User).filter(User.nostr_npub == VIEWER).first()
        user.can_media = True
        db.commit()
    login = connect(api)
    assert api.client.get('/jellyfin/UserViews', headers=headers(login)).status_code == 200
    with Session(api.engine) as db:
        user = db.query(User).filter(User.nostr_npub == VIEWER).first()
        user.can_media = False
        db.commit()
    assert api.client.get('/jellyfin/UserViews', headers=headers(login)).status_code == 401
    api.state['user'] = OWNER
    with Session(api.engine) as db:
        owner = db.query(User).filter(User.nostr_npub == OWNER).first()
        owner.can_media = False
        db.commit()
    assert connect(api)['User']['Id']


def test_connected_devices_metadata_and_individual_revocation(api):
    c = api.client
    pending = c.post('/jellyfin/QuickConnect/Initiate', headers={'X-Emby-Authorization':
        'MediaBrowser Client="Jellyfin Android TV", Device="Living room TV", Version="1.2"'}).json()
    c.post('/api/media-center/jellyfin-account/authorize', json={'code':pending['Code']})
    login = c.post('/jellyfin/Users/AuthenticateWithQuickConnect', json={'secret':pending['Secret']}).json()
    second = connect(api)
    status = c.get('/api/media-center/jellyfin-account').json()
    assert status['sessions'] == 2
    device = status['devices'][0]
    assert device['name'] == 'Living room TV' and device['client'] == 'Jellyfin Android TV'
    assert device['version'] == '1.2' and device['created'] > 0
    assert all('hash' not in row and 'AccessToken' not in row for row in status['devices'])
    api.state['user'] = OWNER
    assert c.delete('/api/media-center/jellyfin-account/devices/'+device['id']).status_code == 404
    assert c.get('/jellyfin/UserViews', headers=headers(login)).status_code == 200
    api.state['user'] = VIEWER
    assert c.delete('/api/media-center/jellyfin-account/devices/'+device['id']).status_code == 204
    assert c.get('/jellyfin/UserViews', headers=headers(login)).status_code == 401
    assert c.get('/jellyfin/UserViews', headers=headers(second)).status_code == 200
    assert c.get('/api/media-center/jellyfin-account').json()['sessions'] == 1


def test_tv_folder_open_preferences_and_private_image_ticket(api, monkeypatch):
    monkeypatch.setenv('POSTERCHANAI_MEDIA_ROOTS', api.library['folder'])
    login = connect(api)
    c, h = api.client, headers(login)
    view = c.get('/jellyfin/UserViews', headers=h).json()['Items'][0]
    folder = c.get('/jellyfin/Items', params={'ParentId':view['Id']}, headers=h).json()['Items'][0]
    for dto in (view, folder):
        # BrowseGridFragment.onCreate calls Objects.requireNonNull on this field.
        assert dto['DisplayPreferencesId'] and dto['DisplayPreferencesId'] == dto['Id']
        detail = c.get('/jellyfin/Items/'+dto['Id'], headers=h).json()
        assert detail['DisplayPreferencesId'] == dto['DisplayPreferencesId']
        prefs = '/jellyfin/DisplayPreferences/'+dto['DisplayPreferencesId']+'?client=android'
        assert c.get(prefs, headers=h).status_code == 200
        assert c.post(prefs, headers=h, json={'CustomPrefs':{'gridDirection':'VERTICAL'}}).status_code == 204
        assert c.get(prefs, headers=h).json()['CustomPrefs']['gridDirection'] == 'VERTICAL'
    url = '/jellyfin/Items/'+folder['Id']+'/Images/Primary'
    tag = folder['ImageTags']['Primary']
    # Image endpoint's normal source validation requires real fixture artwork.
    from PIL import Image
    root = __import__('pathlib').Path(api.library['folder']) / 'Season 2'
    root.mkdir(exist_ok=True)
    Image.new('RGB',(16,16),'blue').save(root/'folder.png')
    assert c.get(url).status_code == 401
    response = c.get(url, params={'tag':tag})
    assert response.status_code == 200, response.text
    assert 'no-store' in response.headers['cache-control']
    assert login['AccessToken'] not in tag
    assert c.get(url, params={'tag':tag[:-1]+('0' if tag[-1]!='0' else '1')}).status_code == 401
    assert c.get('/jellyfin/Items/'+view['Id']+'/Images/Primary', params={'tag':tag}).status_code == 401
    assert c.get('/jellyfin/Items', params={'tag':tag}).status_code == 401
    # A valid picture ticket cannot bypass a sharing change.
    api.catalog['library:'+api.library['id']]['shared_with'] = []
    assert c.get(url, params={'tag':tag}).status_code == 404
    api.catalog['library:'+api.library['id']]['shared_with'] = [VIEWER]
    assert c.get(url, params={'tag':tag}).status_code == 200
    with Session(api.engine) as db:
        viewer = db.query(User).filter(User.nostr_npub == VIEWER).one()
        viewer.can_media = False
        db.commit()
    assert c.get(url, params={'tag':tag}).status_code == 401
    with Session(api.engine) as db:
        db.query(User).filter(User.nostr_npub == VIEWER).one().can_media = True
        db.commit()
    real_time = jf.time.time
    with monkeypatch.context() as patch:
        patch.setattr(jf.time, 'time', lambda: real_time()+7201)
        assert c.get(url, params={'tag':tag}).status_code == 401
    assert c.post('/jellyfin/Sessions/Logout', headers=h).status_code == 204
    assert c.get(url, params={'tag':tag}).status_code == 401


def test_client_preferences_persist_without_account_privilege_escalation(api):
    c = api.client
    login = connect(api)
    h = headers(login)
    path = '/jellyfin/Users/'+login['User']['Id']+'/Configuration'
    assert c.post(path, headers=h, json={'audioLanguagePreference':'jpn','subtitleLanguagePreference':'eng',
                                       'subtitleMode':'Always','IsAdministrator':True}).status_code == 204
    second = connect(api)
    jf._quick.clear(); jf._locators.clear()
    hydrated = c.get('/jellyfin/Users/Me', headers=headers(second)).json()
    assert hydrated['Configuration']['AudioLanguagePreference'] == 'jpn'
    assert hydrated['Configuration']['SubtitleMode'] == 'Always'
    assert not hydrated['Policy']['IsAdministrator']
    assert 'IsAdministrator' not in hydrated['Configuration']
    assert c.post(path, headers=h, json={'SubtitleMode':'broken'}).status_code == 400
    assert c.post(path, headers=h, json={'OrderedViews':['not-a-uuid']}).status_code == 400
    assert c.post(path, headers=h, json={'RememberAudioSelections':'yes'}).status_code == 400
    prefs = '/jellyfin/DisplayPreferences/usersettings?client=android'
    assert c.post(prefs, headers=h, json={'CustomPrefs':{'posterSize':'LARGE'},'ShowBackdrop':False}).status_code == 204
    assert c.get(prefs, headers=headers(second)).json()['CustomPrefs'] == {'posterSize':'LARGE'}
    assert c.get(prefs.replace('android','other'), headers=h).json()['CustomPrefs'] == {}
    assert c.post(prefs, headers=h, json={'CustomPrefs':{'oversized':'x'*9000}}).status_code == 400
    assert c.post(prefs, headers=h, json={'CustomPrefs':{'bad':False}}).status_code == 400
    api.state['user'] = OWNER
    owner = connect(api)
    assert c.get(prefs, headers=headers(owner)).json()['CustomPrefs'] == {}
    assert c.post(path, headers=headers(owner), json={}).status_code == 403


def test_favorites_and_manual_played_state_use_private_shared_history(api):
    c = api.client
    login = connect(api)
    h = headers(login)
    item, info = playable(api, login)
    favorite = '/jellyfin/Users/'+login['User']['Id']+'/FavoriteItems/'+item['Id']
    played = '/jellyfin/Users/'+login['User']['Id']+'/PlayedItems/'+item['Id']
    assert c.post(favorite, headers=h).json()['IsFavorite']
    progress = {'PlaySessionId':info['PlaySessionId'], 'PositionTicks':50000000}
    assert c.post('/jellyfin/Sessions/Playing/Progress', headers=h, json=progress).status_code == 204
    state = c.get('/jellyfin/Items/'+item['Id'], headers=h).json()['UserData']
    assert state['IsFavorite'] and state['PlaybackPositionTicks'] == 50000000
    assert c.get('/jellyfin/Items', headers=h, params={'Filters':'IsFavorite'}).json()['Items'][0]['Id'] == item['Id']
    assert c.post(played, headers=h).json()['Played']
    assert c.get('/jellyfin/UserItems/Resume', headers=h).json()['Items'] == []
    assert c.get('/jellyfin/Items', headers=h, params={'Filters':'IsPlayed'}).json()['TotalRecordCount'] == 1
    assert not c.delete(played, headers=h).json()['Played']
    assert c.get('/jellyfin/Items', headers=h, params={'Filters':'IsUnplayed'}).json()['TotalRecordCount'] == 2
    second = connect(api)
    assert c.get('/jellyfin/Items/'+item['Id'], headers=headers(second)).json()['UserData']['IsFavorite']
    api.state['user'] = OWNER
    owner = connect(api)
    assert not c.get('/jellyfin/Items/'+item['Id'], headers=headers(owner)).json()['UserData']['IsFavorite']
    api.catalog['library:'+api.library['id']]['shared_with'] = []
    assert c.delete(favorite, headers=h).status_code == 404


def test_tv_optional_collections_and_item_extras_are_scoped(api):
    c = api.client
    login = connect(api)
    h = headers(login)
    item, _ = playable(api, login)
    for path in ('/Persons','/Artists','/LiveTv/Programs/Recommended','/Videos/'+item['Id']+'/AdditionalParts',
                 '/MediaSegments/'+item['Id']):
        assert c.get('/jellyfin'+path).status_code == 401
        assert c.get('/jellyfin'+path, headers=h).json()['Items'] == []
    assert c.get('/jellyfin/Items/Filters', headers=h).json()['Genres'] == []
    assert c.get('/jellyfin/Items/'+item['Id']+'/Images', headers=h).json()[0]['ImageType'] == 'Primary'
    assert c.get('/jellyfin/Videos/'+'f'*32+'/AdditionalParts', headers=h).status_code == 404


def test_tv_sparse_stream_indices_do_not_index_past_subtitle_array(api):
    for item in api.entries:
        item['tracks'] = [
            {'index':0,'type':'audio','codec':'aac','language':'jpn','title':'','default':True,'text':False},
            {'index':7,'type':'subtitle','codec':'ass','language':'eng','title':'','default':True,'text':True}]
    login = connect(api)
    item, _ = playable(api, login)
    info = api.client.post('/jellyfin/Items/'+item['Id']+'/PlaybackInfo', headers=headers(login),
                           json={'SubtitleStreamIndex':7}).json()
    source = info['MediaSources'][0]
    assert source['MediaStreams'][source['DefaultSubtitleStreamIndex']]['DeliveryMethod'] == 'External'
    assert source['MediaStreams'][source['DefaultAudioStreamIndex']]['Type'] == 'Audio'
    assert len({s['Index'] for s in source['MediaStreams']}) == len(source['MediaStreams'])
    assert any(s['Type'] == 'Video' for s in source['MediaStreams'])


def test_saved_language_defaults_apply_but_explicit_subtitles_off_wins(api):
    for entry in api.entries:
        entry['tracks'] += [
            {'index':2,'type':'audio','codec':'aac','language':'jpn','title':'','default':False,'text':False},
            {'index':3,'type':'subtitle','codec':'ass','language':'eng','title':'','default':False,'text':True}]
    login = connect(api)
    c, h = api.client, headers(login)
    path = '/jellyfin/Users/'+login['User']['Id']+'/Configuration'
    assert c.post(path, headers=h, json={'AudioLanguagePreference':'jpn','PlayDefaultAudioTrack':False,
                                       'SubtitleLanguagePreference':'eng','SubtitleMode':'Always'}).status_code == 204
    item, info = playable(api, login)
    source = info['MediaSources'][0]
    assert source['DefaultAudioStreamIndex'] == 2 and source['DefaultSubtitleStreamIndex'] == 3
    off = c.post('/jellyfin/Items/'+item['Id']+'/PlaybackInfo', headers=h,
                 json={'AudioStreamIndex':1,'SubtitleStreamIndex':-1}).json()['MediaSources'][0]
    assert off['DefaultAudioStreamIndex'] == 1 and off['DefaultSubtitleStreamIndex'] == -1


@pytest.mark.parametrize('client_name', ['Jellyfin Roku', 'Jellyfin Android TV', 'Jellyfin for Fire TV'])
def test_tv_clients_receive_metadata_source_before_playback(api, client_name):
    login = connect(api)
    h = {'Authorization': 'MediaBrowser Client="'+client_name+'", Token="'+login['AccessToken']+'"'}
    item, _ = playable(api, login)
    c = api.client
    metadata = c.get('/jellyfin/Items/'+item['Id'], headers=h).json()
    assert metadata['MediaSources'][0]['Container'] == 'ts'
    assert metadata['MediaSources'][0]['Protocol'] == 'File'
    assert metadata['MediaSources'][0]['SupportsDirectPlay'] is False
    assert 'TranscodingUrl' not in metadata['MediaSources'][0]  # Browsing never reserves a stream.
    assert any(s['Type']=='Audio' and s['IsDefault'] for s in metadata['MediaStreams'])
    info = c.post('/jellyfin/Items/'+item['Id']+'/PlaybackInfo', headers=h,
                  params={'UserId':login['User']['Id'],'MediaSourceId':item['Id'],
                          'IsPlayback':'true','AudioStreamIndex':1,'MaxStreamingBitrate':140000000},
                  json={'DeviceProfile':{'Name':client_name},'AlwaysBurnInSubtitleWhenTranscoding':True}).json()
    source = info['MediaSources'][0]
    assert not source['IsRemote'] and not source['SupportsDirectPlay'] and source['SupportsTranscoding']
    response = c.get('/jellyfin/'+source['TranscodingUrl'], headers=h)
    assert response.status_code == 200 and response.text.startswith('#EXTM3U')
    images = c.get('/jellyfin/Items/'+item['Id']+'/Images', headers=h).json()
    if client_name == 'Jellyfin Roku':
        assert images == []  # Forces VideoData.setPoster to retain ImageTags.Primary.
    else:
        assert images[0]['ImageType'] == 'Primary'
    # Client-specific rendering choices never grant access to unshared media.
    api.catalog['library:'+api.library['id']]['shared_with'] = []
    assert c.get('/jellyfin/Items/'+item['Id'], headers=h).status_code == 404
    assert c.get('/jellyfin/'+source['TranscodingUrl'], headers=h).status_code == 404


def test_official_roku_brightscript_container_and_poster_runtime(api, tmp_path):
    """Execute release client functions, not a Python imitation of Roku's behavior."""
    import os
    import re
    import subprocess
    from pathlib import Path
    source = os.environ.get('ROKU_TEST_SOURCE')
    interpreter = os.environ.get('ROKU_TEST_BRS')
    if not source or not interpreter:
        pytest.skip('Set ROKU_TEST_SOURCE to official Roku checkout and ROKU_TEST_BRS to brs interpreter')
    source = Path(source)
    loader = (source/'components/ItemGrid/LoadVideoContentTask.bs').read_text()
    container = re.search(r'function getContainerType\(.*?\nend function',loader,re.S).group()
    video_data = (source/'components/data/VideoData.bs').read_text()
    poster = re.search(r'sub setPoster\(.*?\nend sub',video_data,re.S).group()
    login = connect(api)
    item, _ = playable(api, login)
    dto = api.client.get('/jellyfin/Items/'+item['Id'], headers=headers(login)).json()
    literal = json.dumps(dto).replace('"','""')
    code = container+'\n'+poster+'''
function ImageURL(id, version, params)
    return id + "/" + version + "?Tag=" + params.Tag
end function
sub main()
    m.top = {image: invalid}
    m.top.json = ParseJson("'''+literal+'''")
    print "CONTAINER="+getContainerType({json:m.top.json})
    setPoster()
    print "POSTER="+m.top.posterURL
end sub
'''
    path = tmp_path/'roku-contract.brs'
    path.write_text(code)
    result = subprocess.run([interpreter,"-r",str(tmp_path),str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'CONTAINER=ts' in result.stdout
    assert 'POSTER='+dto['Id']+'/Primary?Tag='+dto['ImageTags']['Primary'] in result.stdout
    # The previous empty-array response fails in the actual Roku function.
    broken = code.replace(literal, json.dumps({**dto,'MediaSources':[]}).replace('"','""'))
    path.write_text(broken)
    failed = subprocess.run([interpreter,"-r",str(tmp_path),str(path)],capture_output=True,text=True,timeout=20)
    assert failed.returncode != 0 or 'CONTAINER=ts' not in failed.stdout


def test_existing_device_reopens_saved_video_after_volatile_state_loss(api):
    login = connect(api)
    item, info = playable(api, login)
    c, h = api.client, headers(login)
    old_url = '/jellyfin/' + info['MediaSources'][0]['TranscodingUrl']
    assert c.get(old_url).status_code == 200
    assert c.post('/jellyfin/Sessions/Playing/Progress', headers=h, json={
        'PlaySessionId': info['PlaySessionId'], 'PositionTicks': 60000000,
    }).status_code == 204
    # Model the volatile state lost in a restart, retaining the encrypted store.
    jf._plays.clear()
    jf._locators.clear()
    media._sessions.clear()
    media._catalog_cache.clear()
    media._rate_due.clear()
    assert c.get(old_url).status_code == 404
    # No new Quick Connect approval is needed for the already approved device.
    resumed = c.get('/jellyfin/UserItems/Resume', headers=h)
    assert resumed.status_code == 200
    assert resumed.json()['Items'][0]['UserData']['PlaybackPositionTicks'] == 60000000
    reopened, fresh = playable(api, login)
    assert reopened['Id'] == item['Id']
    assert fresh['PlaySessionId'] != info['PlaySessionId']
    assert c.get('/jellyfin/' + fresh['MediaSources'][0]['TranscodingUrl']).status_code == 200
