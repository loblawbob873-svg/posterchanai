"""Exercise cross-origin and resource limits through actual ASGI requests."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from app.middleware.csrf import CookieOriginMiddleware
from app.middleware.public_work import PublicWorkMiddleware


@pytest.mark.parametrize('origin,authorization,status', [
    ('https://evil.example', '', 403), ('null', '', 403),
    ('https://instance.example', '', 200), ('https://localhost', '', 200),
    ('app://posterchan', '', 200), ('capacitor://localhost', '', 200),
    ('http://instance.example', '', 403), ('https://instance.example.evil', '', 403),
    ('https://evil.example', 'Bearer explicit-credential', 200),
    ('https://evil.example', 'Basic ignored-by-bearer-auth', 403),
    ('https://evil.example', 'Bearer', 403),
])
def test_cookie_origin(origin, authorization, status):
    async def run():
        app = FastAPI()
        @app.post('/mutate')
        async def mutate(): return {'ok': True}
        app.add_middleware(CookieOriginMiddleware)
        headers = {'origin': origin, 'cookie': 'access_token=synthetic'}
        if authorization: headers['authorization'] = authorization
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://instance.example') as client:
            assert (await client.post('/mutate', headers=headers)).status_code == status
    asyncio.run(run())


def test_helper_limits_actual_streamed_body_and_recovers_capacity():
    async def run():
        app = FastAPI()
        @app.post('/client/translate')
        async def translate(request: Request): return {'size': len(await request.body())}
        middleware = PublicWorkMiddleware(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(middleware), base_url='http://test') as client:
            async def body():
                yield b'x' * (128 * 1024)
                yield b'x'
            assert (await client.post('/client/translate', content=body())).status_code == 413
            assert middleware.active == 0
            assert (await client.post('/client/translate', content=b'ok')).json() == {'size': 2}
    asyncio.run(run())


def test_helper_concurrency_is_bounded_before_reading_body():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        count = 0
        app = FastAPI()
        @app.post('/client/translate')
        async def translate():
            nonlocal count
            count += 1
            if count == 2: started.set()
            await release.wait()
            return {'ok': True}
        middleware = PublicWorkMiddleware(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(middleware), base_url='http://test') as client:
            requests = [asyncio.create_task(client.post('/client/translate')) for _ in range(2)]
            try:
                await asyncio.wait_for(started.wait(), 5)
                response = await client.post('/client/translate')
                assert response.status_code == 429
                assert response.headers['retry-after'] == '5'
                assert count == 2
            finally:
                release.set()
                await asyncio.gather(*requests)
            assert middleware.active == 0
    asyncio.run(run())
