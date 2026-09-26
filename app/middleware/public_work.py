"""Bound anonymous helper work before request bodies are parsed or spooled."""
import asyncio
from collections import deque
import time
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class PublicWorkMiddleware:
    LIMITS = {
        '/api/instance-welcome/apply': 12 * 1024,
        '/api/instance-welcome/status': 12 * 1024,
        '/client/translate': 128 * 1024,
        '/client/narrate': 128 * 1024,
        '/client/screenshot': 256 * 1024,
        '/client/stt': 32 * 1024 * 1024,
        '/client/media/compress-video': 513 * 1024 * 1024,
        '/client/summarize': 128 * 1024,
        '/client/compose-from-url': 128 * 1024,
        '/client/hashtags': 128 * 1024,
    }
    # Paths with their OWN concurrency pool instead of the shared 2. Video compression is the one helper
    # that can hand work to another node (client.py: _compress_lb_forward), which only happens when this
    # node's two encoder slots are FULL — and a shared cap of 2 refused the third upload with a 429
    # before the balancer could ever see it. Its own pool also stops two videos from locking translate,
    # speech-to-text and narrate out of the whole node.
    POOLS = {
        '/client/media/compress-video': 4,
    }
    SHARED_CAP = 2

    def __init__(self, app):
        self.app = app
        self.active = 0
        self.pool_active = {}
        self.windows = {}

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope.get('type') != 'http' or scope.get('method') != 'POST' or path not in self.LIMITS:
            return await self.app(scope, receive, send)
        now = time.monotonic()
        for key in list(self.windows):
            queue = self.windows[key]
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if not queue:
                del self.windows[key]
        address = (scope.get('client') or ('unknown',))[0]
        queue = self.windows.get(address, deque())
        pool = self.POOLS.get(path)
        busy = (self.pool_active.get(path, 0) >= pool) if pool else (self.active >= self.SHARED_CAP)
        if busy or len(queue) >= 60 or (address not in self.windows and len(self.windows) >= 4096):
            return await JSONResponse({'detail': 'Helper capacity reached; retry shortly'}, status_code=429,
                headers={'Retry-After': '5', 'Cache-Control': 'no-store'})(scope, receive, send)
        queue.append(now)
        self.windows[address] = queue
        total = 0
        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > self.LIMITS[path]:
                    raise HTTPException(status_code=413, detail='Helper upload too large')
            return message
        if pool:
            self.pool_active[path] = self.pool_active.get(path, 0) + 1
        else:
            self.active += 1
        async def work():
            try:
                await self.app(scope, bounded_receive, send)
            finally:
                if pool:
                    self.pool_active[path] -= 1
                else:
                    self.active -= 1
        task = asyncio.create_task(work())
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        # A disconnected client must not free its capacity while an encoder/thread
        # is still running. The handler's own cleanup releases it when work finishes.
        await asyncio.shield(task)
