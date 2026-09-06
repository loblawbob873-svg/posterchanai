"""Behavioral regressions for the September 2026 security audit."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.services.nostr.event import build_event, verify_self_auth
from app.routers.auth import _verify_nostr_auth
from app.utils import lb_auth


def proof(kind=27235, content='ai-login', **kw):
    event = build_event(bytes.fromhex('01' * 32), kind, content, **kw)
    return base64.b64encode(json.dumps(event).encode()).decode(), event['pubkey']


@pytest.mark.parametrize('kind', [0, 1, 3, 4, 7, 22242, 24242, 30078])
def test_public_events_cannot_authenticate(kind):
    auth, pk = proof(kind)
    assert not _verify_nostr_auth(auth, pk)
    assert not verify_self_auth(auth, pk)


def test_login_requires_its_explicit_purpose_and_preserves_native_proofs():
    auth, pk = proof()
    assert _verify_nostr_auth(auth, pk)
    assert not _verify_nostr_auth(*proof(content='ai-request'))
    assert not _verify_nostr_auth(*proof(content='auth'))
    assert _verify_nostr_auth(*proof(content='ai-request'), 'ai-request')
    assert verify_self_auth(*proof(content='auth'))  # Cached native self-auth capability.
    assert not verify_self_auth(*proof(created_at=1))
    assert not verify_self_auth('x' * 8193, pk)


def test_legacy_password_login_cannot_authenticate_an_administrator():
    from app.routers.auth import login
    from app.schemas import UserLogin
    class DB:
        def query(self, *a): return self
        def filter(self, *a): return self
        def first(self): return SimpleNamespace(is_admin=True)
    with pytest.raises(HTTPException) as exc:
        login(UserLogin(username='admin', password='anything'), Response(), None, DB())
    assert exc.value.status_code == 403
    assert 'Nostr' in exc.value.detail


def test_missing_or_failed_peer_secret_cannot_read_another_users_file(tmp_path, monkeypatch):
    from app.routers import storage
    (tmp_path / 'private.txt').write_text('synthetic private file')
    monkeypatch.setattr(lb_auth, 'shared_secret', lambda: '')
    monkeypatch.setattr(storage, 'StorageService', lambda db: SimpleNamespace(get_user_path=lambda username: tmp_path))
    req = Request({'type': 'http', 'headers': [(b'x-posterchanai-load-balanced', b'true')]})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(storage.view_file(req, 'victim', 'private.txt', True, None, None))
    assert exc.value.status_code == 401
    monkeypatch.setattr(lb_auth, 'shared_secret', lambda: 'synthetic-shared-secret')
    req = Request({'type': 'http', 'headers': [(b'x-posterchanai-load-balanced', b'true'),
                                              (b'x-posterchanai-lb-auth', b'synthetic-shared-secret')]})
    response = asyncio.run(storage.view_file(req, 'victim', 'private.txt', True, None, None))
    assert response.status_code == 200
    assert Path(response.path).read_text() == 'synthetic private file'


@pytest.mark.parametrize('prefix', ['/api/files/shared/', '/api/storage/view-file/', '/blossom/'])
@pytest.mark.parametrize('mime', ['text/html', 'application/xhtml+xml', 'image/svg+xml'])
def test_untrusted_documents_have_an_opaque_sandboxed_origin(prefix, mime):
    from app.middleware.untrusted_files import UntrustedFilesMiddleware
    app = FastAPI()
    app.add_middleware(UntrustedFilesMiddleware)
    @app.get(prefix + 'example')
    def document(): return Response('<script>document.title="audit"</script>', media_type=mime)
    with TestClient(app) as client:
        response = client.get(prefix + 'example')
    policy = response.headers['content-security-policy']
    assert 'sandbox;' in policy and 'allow-same-origin' not in policy and 'allow-scripts' not in policy
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.status_code == 200


@pytest.mark.parametrize('destination,expected_calls', [('http://127.0.0.1:9999/private', 1),
                                                       ('https://public.example/final', 2)])
def test_screenshot_validates_each_redirect(monkeypatch, destination, expected_calls):
    from app.routers import client
    from app.services.command_service import _common
    seen = []
    def transport(request):
        seen.append(str(request.url))
        if request.url.path == '/avatar':
            return httpx.Response(302, headers={'location': destination})
        return httpx.Response(200, content=b'synthetic image', headers={'content-type': 'image/png'})
    real = httpx.AsyncClient
    monkeypatch.setattr(_common, '_url_is_safe_to_fetch', lambda url, hosts: url.startswith('https://public.example/'))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(transport), **kw))
    result = asyncio.run(client._img_data_uri('https://public.example/avatar', 'poster.example'))
    assert len(seen) == expected_calls
    assert bool(result) == (expected_calls == 2)


@pytest.mark.parametrize('web_page', [False, True])
def test_admin_email_verification_never_creates_a_login_cookie(web_page):
    from datetime import datetime, timedelta
    from app.main import verify_email_page
    from app.routers.auth import verify_email
    user = SimpleNamespace(id=4, is_admin=True, email_verified=False)
    verification = SimpleNamespace(user_id=4, expires_at=datetime.utcnow() + timedelta(minutes=5))
    class DB:
        def __init__(self): self.rows = iter([verification, user]); self.deleted = []
        def query(self, *args): return self
        def filter(self, *args): return self
        def first(self): return next(self.rows)
        def delete(self, row): self.deleted.append(row)
        def commit(self): pass
    db, response = DB(), Response()
    if web_page:
        result = asyncio.run(verify_email_page('synthetic', response, db))
        assert 'set-cookie' not in result.headers and result.status_code == 302
    else:
        result = verify_email('synthetic', response, db)
        assert 'Nostr' in result['message']
    assert 'set-cookie' not in response.headers
    assert user.email_verified and db.deleted == [verification]


def test_stream_application_uses_its_own_request_fields():
    from app.models import User
    from app.routers.client import StreamRequestReq, stream_request
    auth, pk = proof(content='auth')
    user = SimpleNamespace(id=5, username='fixture', is_admin=False, can_stream=False)
    class DB:
        def __init__(self): self.added = []
        def query(self, model): self.model = model; return self
        def filter(self, *args): return self
        def first(self): return user if self.model is User else None
        def add(self, row): self.added.append(row)
        def commit(self): pass
    db = DB()
    result = asyncio.run(stream_request(StreamRequestReq(pubkey=pk, auth=auth), db))
    assert result.status_code == 200 and db.added[0].key == 'stream_requested'


def test_files_index_rejects_conflicting_operations_before_loading_user():
    from app.routers.client import FilesIndexReq, files_index
    auth, pk = proof(content='auth')
    result = asyncio.run(files_index(FilesIndexReq(pubkey=pk, auth=auth, index={}, history=True), None))
    assert result.status_code == 400


def test_remote_xml_entities_are_rejected(monkeypatch):
    from app.routers import news, office
    payload = b'<!DOCTYPE rss [<!ENTITY injected "EXPANDED">]><rss><channel><item><title>&injected;</title><link>https://example.test/</link></item></channel></rss>'
    links, error = news._parse_rss_feed(payload, 'https://example.test')
    assert links == [] and error and 'EntitiesForbidden' in error
    good = b'<rss><channel><item><title>News</title><link>https://example.test/</link></item></channel></rss>'
    assert news._parse_rss_feed(good, 'https://example.test') == (['- [News](https://example.test/)'], None)
    async def discover(client):
        return SimpleNamespace(content=b'<!DOCTYPE wopi-discovery [<!ENTITY injected "EXPANDED">]><wopi-discovery>&injected;</wopi-discovery>')
    monkeypatch.setattr(office, '_discover', discover)
    monkeypatch.setattr(office, '_exts_cache', None)
    assert asyncio.run(office._accepted_exts()) == frozenset(office._EXTS_FALLBACK)
    with pytest.raises(HTTPException):
        asyncio.run(office._action_url('docx', 'edit'))
