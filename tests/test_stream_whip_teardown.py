"""WHIP TEARDOWN: the publisher can END its session (RFC 9725 DELETE), not only abandon it.

Run: venv-unified/bin/python -m pytest tests/test_stream_whip_teardown.py

PBS (docs/PBS.md) ends a broadcast with Stop. Closing the RTCPeerConnection alone leaves MediaMTX's path "ready"
until it notices the dead WebRTC session, and for those seconds the stream still reads as live to the reaper and to
viewers. So the WHIP proxy now hands back MediaMTX's session as OUR resource (`Location: /api/streams/whip/<token>/<uuid>`
-- never the internal 127.0.0.1 URL) and DELETE on it is forwarded to MediaMTX with the owner's key. Same ownership
rule as the POST; the session id is spliced into an upstream URL, so anything but a UUID is refused before any
request leaves the box.
"""
import httpx
import pytest

from app.services import settings_store
from tests import test_stream_auth_hook as _hook
from tests.test_stream_auth_hook import ALICE, BOB, OFFER

node = _hook.node   # the auth-hook suite's node fixture: a seeded user table, settings and a TestClient

SID = '3f2b8c1e-9a4d-4c2e-8f1a-0123456789ab'


class _Upstream:
    def __init__(self, monkeypatch, location='/%s/whip/' + SID, delete_status=200):
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
                u = httpx.URL(url)
                outer.sent.append(('POST', u))
                tok = u.path.split('/')[1]
                return httpx.Response(201, content=b'v=0\r\n', headers={
                    'content-type': 'application/sdp', 'location': (location % tok) if '%s' in location else location})

            async def delete(self, url):
                outer.sent.append(('DELETE', httpx.URL(url)))
                return httpx.Response(delete_status)
        monkeypatch.setattr(httpx, 'AsyncClient', Client)


def test_the_answer_names_our_session_resource_never_mediamtx_internal_url(node, monkeypatch):
    _Upstream(monkeypatch, location='http://127.0.0.1:8889/%s/whip/' + SID)
    tok = node.token[ALICE]
    r = node.client.post('/api/streams/whip/' + tok, content=OFFER, headers={'Content-Type': 'application/sdp'})
    assert r.status_code == 201, r.text
    assert r.headers.get('location') == '/api/streams/whip/%s/%s' % (tok, SID)
    assert '127.0.0.1' not in r.headers.get('location', '')


def test_an_answer_without_a_session_gets_no_location(node, monkeypatch):
    _Upstream(monkeypatch, location='')
    r = node.client.post('/api/streams/whip/' + node.token[ALICE], content=OFFER, headers={'Content-Type': 'application/sdp'})
    assert r.status_code == 201 and 'location' not in r.headers


def test_delete_ends_the_owners_session_on_this_machines_mediamtx(node, monkeypatch):
    up = _Upstream(monkeypatch)
    tok = node.token[ALICE]
    r = node.client.delete('/api/streams/whip/%s/%s' % (tok, SID))
    assert r.status_code == 200, r.text
    [(verb, u)] = up.sent
    assert verb == 'DELETE' and (u.host, u.port, u.path) == ('127.0.0.1', 8889, '/%s/whip/%s' % (tok, SID))
    assert u.params.get('key') == node.key[ALICE]


def test_a_session_that_is_already_gone_is_ended_not_an_error(node, monkeypatch):
    _Upstream(monkeypatch, delete_status=404)
    assert node.client.delete('/api/streams/whip/%s/%s' % (node.token[ALICE], SID)).status_code == 200


@pytest.mark.parametrize('junk', ['..', 'x' * 36, SID + '/../../v3/config', '3f2b8c1e-9a4d-4c2e-8f1a-0123456789ab%3Fkey%3D1'])
def test_only_a_uuid_ever_reaches_the_upstream_url(node, monkeypatch, junk):
    up = _Upstream(monkeypatch)
    r = node.client.delete('/api/streams/whip/%s/%s' % (node.token[ALICE], junk))
    assert r.status_code in (400, 403, 404, 405), r.status_code
    assert up.sent == []


def test_nobody_ends_somebody_elses_stream(node, monkeypatch):
    up = _Upstream(monkeypatch)
    node.current['uid'] = BOB
    assert node.client.delete('/api/streams/whip/%s/%s' % (node.token[ALICE], SID)).status_code == 403
    monkeypatch.setitem(settings_store._CACHE, 'stream_enabled', 'false')
    node.current['uid'] = ALICE
    assert node.client.delete('/api/streams/whip/%s/%s' % (node.token[ALICE], SID)).status_code == 404
    assert up.sent == []
