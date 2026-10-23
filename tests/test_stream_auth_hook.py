"""THE PUBLISH BOUNDARY OF LIVE STREAMING: MediaMTX's auth hook, its end-of-stream hook and the WHIP proxy.

MediaMTX asks POST /api/streams/auth?hook=<secret> before it lets anybody publish (or read) a path; the app answers
200 to allow and anything else to refuse. Everything a stream key protects is decided here, so every way to get it
wrong is a case below: a forged or missing hook secret (the public app as a key-validity oracle), no key, a key
nobody was given, a REAL key used on somebody else's path, a made-up path, a sub-path of a victim's token, a key whose
owner lost the permission, a deactivated key, the bitrate clamp's own publish without (or with a wrong) clamp secret,
and a key table that cannot be read (a 503, never "wrong key"). Then the unpublish hook and the WHIP proxy (the
browser's Go Live), which must refuse somebody else's token and send the key ONLY to this machine's MediaMTX.

No MediaMTX here -- the routes are called the way MediaMTX calls them. The same refusals AGAINST THE REAL MediaMTX
(an RTMP and a WHIP publish with forged and borrowed keys) are tests/client/test_golive_real_mediamtx.py.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth as app_auth
from app.database import get_db
from app.routers import streams
from app.services import api_key_store, settings_store, stream_end_service, stream_service, user_settings_table, \
    user_table
from tests.doc_table_mem import mem_tables
from tests.user_fixtures import seed_user

SECRET = 'h' * 48
ALICE, BOB, MALLORY = 1, 2, 3          # Bob may stream; Mallory has an account, a key, and no permission


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def node(monkeypatch):
    mem = mem_tables(monkeypatch)
    for uid, name, can in ((ALICE, 'alice', True), (BOB, 'bob', True), (MALLORY, 'mallory', False)):
        seed_user(mem, uid, username=name, can_stream=can, nostr_npub='%064x' % uid)
    for k, v in {'stream_enabled': 'true', 'stream_auth_secret': SECRET, 'stream_webrtc_port': '8889'}.items():
        monkeypatch.setitem(settings_store._CACHE, k, v)

    class N:
        pass
    n = N()
    n.mem = mem
    n.token, n.key = {}, {}
    for uid in (ALICE, BOB, MALLORY):
        tok = 'tok%d' % uid + 'a' * 13
        run(user_settings_table.aset(None, uid, 'stream_token', tok))
        n.token[uid] = tok
        n.key[uid] = run(api_key_store.acreate(uid, 'OBS Stream')).key
    app = FastAPI()
    app.include_router(streams.router)
    current = {'uid': ALICE}

    def who(request: Request):
        return user_table.get(None, current['uid'])
    app.dependency_overrides[app_auth.get_current_user] = who
    app.dependency_overrides[get_db] = lambda: None
    n.current = current
    n.client = TestClient(app)
    n.ended = []
    monkeypatch.setattr(stream_end_service, 'schedule_end', lambda tok, uid: n.ended.append((tok, uid)))
    monkeypatch.setattr(stream_end_service, 'mark_publishing', lambda db, uid: asyncio.sleep(0))
    return n


def hook(n, body, secret=SECRET, path='/api/streams/auth'):
    url = path if secret is None else '%s?hook=%s' % (path, secret)
    return n.client.post(url, content=json.dumps(body), headers={'Content-Type': 'application/json'})


def publish(n, path, query='', **extra):
    return hook(n, dict({'action': 'publish', 'path': path, 'query': query, 'protocol': 'rtmp', 'ip': '203.0.113.9'},
                        **extra))


# ------------------------------------------------------------------------------------------------ the hook itself
def test_the_owner_publishes_to_their_own_path(node):
    assert publish(node, node.token[ALICE], 'key=' + node.key[ALICE]).status_code == 200
    # The two other shapes a real encoder sends: the Android app's bare key, and a stray separator glued on.
    assert publish(node, node.token[ALICE], node.key[ALICE]).status_code == 200
    assert publish(node, node.token[ALICE], '?key=%s&' % node.key[ALICE]).status_code == 200


@pytest.mark.parametrize('secret', [None, '', 'x' * 48, SECRET[:-1], SECRET + 'x'])
def test_a_missing_or_forged_hook_secret_is_refused_before_anything_is_looked_at(node, secret):
    r = hook(node, {'action': 'publish', 'path': node.token[ALICE], 'query': 'key=' + node.key[ALICE]}, secret=secret)
    assert r.status_code == 403, r.text
    r = hook(node, {'action': 'read', 'path': node.token[ALICE]}, secret=secret)
    assert r.status_code == 403, 'the public app answered a read for a caller that is not our MediaMTX'


def test_a_node_with_no_hook_secret_refuses_everything(node, monkeypatch):
    monkeypatch.setitem(settings_store._CACHE, 'stream_auth_secret', '')
    assert hook(node, {'action': 'publish', 'path': node.token[ALICE], 'query': 'key=' + node.key[ALICE]},
                secret='').status_code == 403


def test_reading_is_public_and_every_other_action_is_refused(node):
    assert hook(node, {'action': 'read', 'path': node.token[ALICE]}).status_code == 200
    assert hook(node, {'action': 'playback', 'path': node.token[ALICE]}).status_code == 200
    for action in ('api', 'metrics', 'pprof', 'publishx', '', 'PUBLISH-ALL'):
        assert hook(node, {'action': action, 'path': node.token[ALICE]}).status_code in (401, 403), action


def test_no_key_or_a_key_nobody_was_given_is_refused(node):
    assert publish(node, node.token[ALICE]).status_code == 401
    assert publish(node, node.token[ALICE], 'key=').status_code == 401
    assert publish(node, node.token[ALICE], 'key=sk-' + 'f' * 64).status_code == 401
    assert publish(node, node.token[ALICE], 'key=' + node.key[ALICE][:-1]).status_code == 401


def test_a_real_key_cannot_publish_to_somebody_elses_stream(node):
    """Bob holds a valid key. With it he must not be able to take over Alice's path -- the one her viewers watch."""
    assert publish(node, node.token[ALICE], 'key=' + node.key[BOB]).status_code == 403
    assert publish(node, node.token[ALICE], node.key[BOB]).status_code == 403
    assert publish(node, node.token[ALICE], password=node.key[BOB]).status_code == 403


@pytest.mark.parametrize('path', ['', 'made-up', 'tok1', '%s/sub' % ('tok1' + 'a' * 13), 'tok1' + 'a' * 13 + 'x',
                                  ('tok1' + 'a' * 13).upper()])
def test_a_real_key_cannot_publish_to_a_made_up_or_sub_path(node, path):
    assert publish(node, path, 'key=' + node.key[ALICE]).status_code in (401, 403), path


def test_a_key_whose_owner_has_no_stream_permission_is_refused(node):
    assert publish(node, node.token[MALLORY], 'key=' + node.key[MALLORY]).status_code == 403
    # ...and a permission taken away AFTER the key was pasted into OBS stops the next publish.
    u = user_table.get(None, BOB)
    u.can_stream = False
    user_table.save(None, u)
    assert publish(node, node.token[BOB], 'key=' + node.key[BOB]).status_code == 403


def test_a_deactivated_key_is_refused(node):
    row = api_key_store.lookup(node.key[ALICE])
    api_key_store.set_active(row.id, False)
    assert publish(node, node.token[ALICE], 'key=' + node.key[ALICE]).status_code == 401


def test_a_key_table_that_cannot_be_read_is_try_again_not_wrong_key(node, monkeypatch):
    async def down(key):
        from app.services.relay_reader import Unavailable
        raise Unavailable('test')
    monkeypatch.setattr(api_key_store, 'alookup', down)
    assert publish(node, node.token[ALICE], 'key=' + node.key[ALICE]).status_code == 503


def test_the_clamp_publishes_only_with_its_secret_and_only_onto_a_real_stream(node):
    clamped = node.token[ALICE] + stream_service.CLAMP_SUFFIX
    good = stream_service.clamp_secret()
    assert good
    assert publish(node, clamped, 'clamp=' + good, protocol='rtsp').status_code == 200
    assert publish(node, clamped, protocol='rtsp').status_code == 403
    assert publish(node, clamped, 'clamp=' + 'x' * 32, protocol='rtsp').status_code == 403
    assert publish(node, clamped, 'clamp=' + good[:-1], protocol='rtsp').status_code == 403
    # A real user key is not a clamp secret, and the clamp secret does not open arbitrary paths.
    assert publish(node, clamped, 'key=' + node.key[ALICE]).status_code == 403
    assert publish(node, 'nobody' + stream_service.CLAMP_SUFFIX, 'clamp=' + good, protocol='rtsp').status_code == 403
    assert publish(node, node.token[ALICE], 'clamp=' + good).status_code in (401, 403)


# ------------------------------------------------------------------------------------------------ the unpublish hook
def test_the_unpublish_hook_needs_the_secret_and_ends_only_a_real_stream(node):
    for secret in (None, '', 'x' * 48):
        r = hook(node, {}, secret=secret, path='/api/streams/unpublish')
        assert r.status_code == 403
    assert node.ended == []
    r = node.client.post('/api/streams/unpublish?hook=%s&path=%s' % (SECRET, node.token[BOB]))
    assert r.status_code == 200 and node.ended == [(node.token[BOB], BOB)]
    node.client.post('/api/streams/unpublish?hook=%s&path=%s' % (SECRET, 'nobody'))
    node.client.post('/api/streams/unpublish?hook=%s&path=%s%s' % (SECRET, node.token[BOB], stream_service.CLAMP_SUFFIX))
    assert node.ended == [(node.token[BOB], BOB)], 'an unknown or clamped path scheduled an end'


# ------------------------------------------------------------------------------------------------ the WHIP proxy
class _Upstream:
    """What the WHIP proxy sends to MediaMTX -- recorded, answered with an SDP."""

    def __init__(self, monkeypatch):
        import httpx
        self.sent = []
        outer = self

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, content=None, headers=None):
                outer.sent.append(httpx.URL(url))
                return httpx.Response(201, content=b'v=0\r\n', headers={'content-type': 'application/sdp'})
        monkeypatch.setattr(httpx, 'AsyncClient', Client)


OFFER = b'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\n'


def whip(n, token):
    return n.client.post('/api/streams/whip/' + token, content=OFFER, headers={'Content-Type': 'application/sdp'})


def test_whip_refuses_somebody_elses_token_and_a_user_without_permission(node, monkeypatch):
    up = _Upstream(monkeypatch)
    node.current['uid'] = BOB
    assert whip(node, node.token[ALICE]).status_code == 403
    assert whip(node, 'made-up').status_code == 403
    node.current['uid'] = MALLORY
    assert whip(node, node.token[MALLORY]).status_code == 403
    assert up.sent == [], 'a refused go-live still reached MediaMTX'
    monkeypatch.setitem(settings_store._CACHE, 'stream_enabled', 'false')
    node.current['uid'] = ALICE
    assert whip(node, node.token[ALICE]).status_code == 404
    assert up.sent == []


def test_whip_sends_the_owners_key_to_this_machines_mediamtx(node, monkeypatch):
    up = _Upstream(monkeypatch)
    r = whip(node, node.token[ALICE])
    assert r.status_code == 201, r.text
    [u] = up.sent
    assert (u.host, u.port, u.path) == ('127.0.0.1', 8889, '/%s/whip' % node.token[ALICE])
    assert u.params.get('key') == node.key[ALICE]


@pytest.mark.parametrize('junk', ['1@203.0.113.7', '8889@evil.example', '8889/../x', '8889²', '٨٨٨٩', '0', '99999',
                                  ' 8889\nauthMethod: internal'])
def test_whip_never_sends_the_key_anywhere_but_the_port_mediamtx_was_given(node, monkeypatch, junk):
    """`stream_webrtc_port` syncs from the relay, so it is not a trusted string -- the same reasoning that validates the
    HLS and API ports. The generated config falls back to 8889 for a junk value; the WHIP proxy must use the SAME
    port, or (a) every go-live 502s against a port MediaMTX never bound and (b) a value like `1@host` makes the proxy
    POST the streamer's key to that host."""
    monkeypatch.setitem(settings_store._CACHE, 'stream_webrtc_port', junk)
    up = _Upstream(monkeypatch)
    whip(node, node.token[ALICE])
    assert up.sent, 'nothing reached MediaMTX'
    [u] = up.sent
    assert u.host == '127.0.0.1' and not u.userinfo, ('the key went to another host', str(u))
    assert u.port == 8889, ('the proxy used a port the generated config does not bind', str(u))


# ------------------------------------------------------------------------------------------------ is anybody there
def test_ingest_says_whether_the_media_server_is_answering(node, monkeypatch):
    """Go Live asks this before announcing for OBS: an announcement with MediaMTX down is a stream nobody can reach."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    closed = s.getsockname()[1]
    s.close()
    monkeypatch.setitem(settings_store._CACHE, 'stream_api_port', str(closed))
    r = node.client.get('/api/streams/ingest')
    assert r.status_code == 200 and r.json()['server_up'] is False, r.text

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setitem(settings_store._CACHE, 'stream_api_port', str(srv.server_address[1]))
        assert node.client.get('/api/streams/ingest').json()['server_up'] is True
        monkeypatch.setitem(settings_store._CACHE, 'stream_enabled', 'false')
        assert node.client.get('/api/streams/ingest').json()['server_up'] is False
    finally:
        srv.shutdown()
        srv.server_close()
