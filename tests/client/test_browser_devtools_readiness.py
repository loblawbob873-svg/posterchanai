"""Real loopback HTTP boundary: a Chrome port file does not mean /json is ready."""
import asyncio
import io
import json
from types import SimpleNamespace
import pytest
from tests.client.test_desktop_offline_full_app import wait_browser_target


async def exercise(responses, *, timeout=1):
    tasks=set();calls=[]
    async def handle(reader,writer):
        task=asyncio.current_task();tasks.add(task)
        try:
            await reader.readuntil(b'\r\n\r\n')
            call=len(calls);calls.append(call)
            response=responses[min(call,len(responses)-1)]
            if response=='stall':
                await asyncio.sleep(10)
                return
            status,body=response
            data=json.dumps(body).encode()
            writer.write(f'HTTP/1.1 {status} Result\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode()+data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)
    server=await asyncio.start_server(handle,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    try:
        target=await wait_browser_target(SimpleNamespace(poll=lambda:None),port,
                                        io.BytesIO(b'fixture Chrome startup detail'),
                                        timeout=timeout,request_timeout=.04)
        return target,calls
    finally:
        server.close()
        pending=list(tasks)
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        await server.wait_closed()


def test_discovery_retries_stalled_http_and_incomplete_target_list():
    target,calls=asyncio.run(exercise(['stall',(503,{}),(200,[]),
        (200,[{'type':'page','webSocketDebuggerUrl':'ws://127.0.0.1:1234/devtools/page/test'}])]))
    assert target=='ws://127.0.0.1:1234/devtools/page/test'
    assert len(calls)==4


def test_stalled_discovery_is_bounded_and_keeps_startup_evidence():
    async def check():
        start=asyncio.get_running_loop().time()
        with pytest.raises(AssertionError,match='Chrome DevTools discovery failed') as failure:
            await exercise(['stall'],timeout=.23)
        assert asyncio.get_running_loop().time()-start<1
        text=str(failure.value)
        assert 'timeout=0.23s' in text and 'Timeout' in text
        assert 'fixture Chrome startup detail' in text and 'attempts=' in text
    asyncio.run(check())


def test_exited_browser_is_reported_without_retrying_http():
    with pytest.raises(AssertionError,match='exit=17.*attempts=0.*crash diagnostic'):
        asyncio.run(wait_browser_target(SimpleNamespace(poll=lambda:17),1,
                                       io.BytesIO(b'crash diagnostic'),timeout=.1))


@pytest.fixture(scope='module')
def bundled_assets():
    from tests.client import test_desktop_offline_full_app as desktop
    yield from desktop.bundle.__wrapped__()


def test_shared_browser_fixture_recovers_from_initial_discovery_timeout(monkeypatch,bundled_assets):
    from pathlib import Path
    import httpx
    from tests.client import test_desktop_offline_full_app as desktop
    if not Path('/opt/google/chrome/chrome').exists():
        pytest.skip('Chrome required')
    original=httpx.AsyncClient.get
    attempts=[]
    async def get(client,url,*args,**kwargs):
        if str(url).endswith('/json'):
            attempts.append(str(url))
            if len(attempts)==1:
                raise httpx.ReadTimeout('cold Chrome has not answered discovery yet')
        return await original(client,url,*args,**kwargs)
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    checked=[]
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        assert await browser.js('!!window.__PC && !!window.PCOS')
        checked.append(True)
    asyncio.run(desktop.with_browser('online','',check))
    assert len(attempts)>=2
    assert checked==[True]
