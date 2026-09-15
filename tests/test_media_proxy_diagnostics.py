"""Stream the real proxy response without a network, DB connection, or buffered body."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

def _load_proxy_methods():
    # Load the exact route implementation with inert authentication/database boundaries.
    # The minimal release gate has no PostgreSQL driver and must never connect to a real DB.
    import ast
    import logging
    import time
    from pathlib import Path
    from types import ModuleType
    from fastapi import Depends, HTTPException
    from fastapi.responses import StreamingResponse
    from urllib.parse import urlsplit
    from app.services import media_center as media
    source = Path(__file__).resolve().parents[1] / 'app/routers/media_center.py'
    names = {'ProxiedResponse', '_segment_diagnostic', '_log_segment_delivery', 'proxy_request'}
    tree = ast.parse(source.read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names]
    assert {node.name for node in selected} == names
    module = ModuleType('app.routers.media_center')
    module.__dict__.update(asyncio=asyncio, logging=logging, time=time, httpx=httpx, media=media,
        Request=Request, Depends=Depends, HTTPException=HTTPException, StreamingResponse=StreamingResponse,
        urlsplit=urlsplit, _proxy_client=None, media_user_optional=lambda: None, get_db=lambda: None,
        settings_store=SimpleNamespace(get=lambda *_: ''),
        lb_auth=SimpleNamespace(shared_secret=lambda: '', headers=lambda h: h),
        instance_membership=SimpleNamespace(require_user=None), ticket_user=lambda *_: None)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), module.__dict__)
    return module


routes = _load_proxy_methods()

SECRET = 'secret-ticket-viewer-library-item-upstream-address'


@pytest.fixture
def proxy(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    # Patch this router's clock only, never asyncio's event-loop monotonic clock.
    monkeypatch.setattr(routes, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(routes.settings_store, 'get', lambda *args: 'http://' + SECRET)
    monkeypatch.setattr(routes.lb_auth, 'shared_secret', lambda: SECRET)
    monkeypatch.setattr(routes.lb_auth, 'headers', lambda h: h)
    monkeypatch.setattr(routes, 'ticket_user', lambda *args: SimpleNamespace(is_admin=False, can_media=True))
    monkeypatch.setattr(routes.media, 'identity', lambda _: SECRET)
    async def allowed(_): pass
    monkeypatch.setattr(routes.instance_membership, 'require_user', allowed)

    class Upstream:
        status_code = 200
        headers = {'Content-Type': 'video/mp2t', 'Content-Length': '6'}
        closed = 0
        reads = 0
        error = None
        close_error = None
        async def aiter_raw(self):
            self.reads += 1
            yield b'abc'
            if self.error:
                raise self.error
            self.reads += 1
            yield b'def'
        async def aclose(self):
            self.closed += 1
            if self.close_error:
                raise self.close_error
    upstream = Upstream()

    class Client:
        error = None
        header_delay = 1.0
        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)
        async def send(self, request, **kwargs):
            assert kwargs == {'stream': True}
            clock.now += self.header_delay
            if self.error:
                raise self.error
            return upstream
    client = Client()
    monkeypatch.setattr(routes, '_proxy_client', client)

    async def open_response(asset='720p-7.ts'):
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        path=f'/api/media-center/{SECRET}/hls/{SECRET}/{asset}'
        request=Request({'type':'http','method':'GET','scheme':'https','server':('public.test',443),
                         'path':path,'query_string':f'ticket={SECRET}&viewer={SECRET}'.encode(),
                         'headers':[]}, receive)
        with pytest.raises(routes.ProxiedResponse) as caught:
            await routes.proxy_request(request, user=None, db=None)
        return caught.value.response
    return clock, upstream, client, open_response


def messages(caplog):
    return [r for r in caplog.records if r.name == routes.__name__]


def check_private(record):
    text=record.getMessage()
    assert SECRET not in text
    assert 'http://' not in text and 'ticket=' not in text and 'viewer=' not in text
    assert record.exc_info is None
    assert len(text) < 250
    return text


@pytest.mark.parametrize('elapsed,expected', [(2,False),(6,False),(6.01,True)])
def test_successful_stream_logs_only_when_slower_than_a_segment(proxy,caplog,elapsed,expected):
    clock, upstream, _, open_response=proxy
    async def check():
        response=await open_response()
        assert response.status_code==200 and response.headers['content-type']=='video/mp2t'
        assert upstream.reads==0  # no prefetch/body buffering
        iterator=response.body_iterator
        assert await anext(iterator)==b'abc'
        assert upstream.reads==1  # consumer controls backpressure
        clock.now=10+elapsed
        assert await anext(iterator)==b'def'
        with pytest.raises(StopAsyncIteration): await anext(iterator)
    asyncio.run(check())
    assert upstream.closed==1
    records=messages(caplog)
    assert len(records)==int(expected)
    if expected:
        text=check_private(records[0])
        assert 'headers_s=1.000' in text and 'elapsed_s=6.010' in text
        assert 'upstream_bytes=6 completed=True' in text and 'outcome=complete status=200' in text
        assert 'profile=720p number=7' in text


@pytest.mark.parametrize('failure', ['cancel','error','close'])
def test_interrupted_stream_closes_once_and_preserves_exception(proxy,caplog,failure):
    clock, upstream, _, open_response=proxy
    if failure=='cancel': upstream.error=asyncio.CancelledError(SECRET)
    if failure=='error': upstream.error=httpx.ReadError(SECRET)
    async def check():
        response=await open_response()
        assert await anext(response.body_iterator)==b'abc'
        clock.now=12
        if failure=='close':
            await response.body_iterator.aclose()
        else:
            with pytest.raises(type(upstream.error)): await anext(response.body_iterator)
    asyncio.run(check())
    assert upstream.closed==1
    records=messages(caplog); assert len(records)==1
    text=check_private(records[0])
    assert 'upstream_bytes=3 completed=False' in text
    assert 'outcome='+{'cancel':'cancelled','error':'stream_error','close':'interrupted'}[failure] in text


@pytest.mark.parametrize('status',[403,503])
def test_fast_upstream_http_error_is_logged_without_changing_response(proxy,caplog,status):
    _, upstream, _, open_response=proxy
    upstream.status_code=status
    async def check():
        response=await open_response()
        assert response.status_code==status
        assert b''.join([c async for c in response.body_iterator])==b'abcdef'
    asyncio.run(check())
    assert upstream.closed==1
    records=messages(caplog); assert len(records)==1
    assert f'outcome=upstream_status status={status}' in check_private(records[0])


@pytest.mark.parametrize('cancel',[False,True])
def test_request_send_failure_logs_no_exception_secrets(proxy,caplog,cancel):
    _, upstream, client, open_response=proxy
    client.error=asyncio.CancelledError(SECRET) if cancel else httpx.ConnectError(SECRET)
    async def check():
        with pytest.raises(asyncio.CancelledError if cancel else routes.HTTPException) as caught:
            await open_response()
        if not cancel: assert caught.value.status_code==502
    asyncio.run(check())
    assert upstream.closed==0  # send never returned an owned response
    records=messages(caplog); assert len(records)==1
    text=check_private(records[0])
    assert 'headers_s=-1.000' in text and 'status=None' in text and 'upstream_bytes=0 completed=False' in text


@pytest.mark.parametrize('asset',['master.m3u8','private-name-7.ts','720p-'+SECRET+'.ts','720p-999999999999999999999999.ts'])
def test_unrecognized_assets_never_enter_segment_logs(proxy,caplog,asset):
    clock, upstream, _, open_response=proxy
    async def check():
        response=await open_response(asset)
        clock.now=100
        assert b''.join([c async for c in response.body_iterator])==b'abcdef'
    asyncio.run(check())
    assert upstream.closed==1
    assert not messages(caplog)


def test_close_failure_is_logged_once_without_masking_the_exception(proxy,caplog):
    _, upstream, _, open_response=proxy
    error=RuntimeError(SECRET)
    upstream.close_error=error
    async def check():
        response=await open_response()
        with pytest.raises(RuntimeError) as caught:
            _=[c async for c in response.body_iterator]
        assert caught.value is error
    asyncio.run(check())
    assert upstream.closed==1
    records=messages(caplog);assert len(records)==1
    text=check_private(records[0])
    assert 'outcome=close_error' in text and 'upstream_bytes=6 completed=True' in text


def test_actual_task_cancellation_closes_the_stream(proxy,caplog):
    _, upstream, _, open_response=proxy
    async def check():
        started=asyncio.Event()
        async def waiting_body():
            yield b'abc'
            started.set()
            await asyncio.Event().wait()
        upstream.aiter_raw=waiting_body
        response=await open_response()
        async def consume():
            async for _ in response.body_iterator: pass
        task=asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(check())
    assert upstream.closed==1
    records=messages(caplog);assert len(records)==1
    assert 'outcome=cancelled' in check_private(records[0])


def test_slow_headers_are_distinguished_from_a_fast_body(proxy,caplog):
    _, upstream, client, open_response=proxy
    client.header_delay=8.0
    async def check():
        response=await open_response()
        assert b''.join([c async for c in response.body_iterator])==b'abcdef'
    asyncio.run(check())
    assert upstream.closed==1
    records=messages(caplog);assert len(records)==1
    text=check_private(records[0])
    assert 'headers_s=8.000 elapsed_s=8.000' in text
    assert 'upstream_bytes=6 completed=True' in text
