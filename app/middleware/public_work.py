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

    def __init__(self, app):
        self.app = app
        self.active = 0
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
        if self.active >= 2 or len(queue) >= 60 or (address not in self.windows and len(self.windows) >= 4096):
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
        self.active += 1
        async def work():
            try:
                await self.app(scope, bounded_receive, send)
            finally:
                self.active -= 1
        task = asyncio.create_task(work())
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        # A disconnected client must not free its capacity while an encoder/thread
        # is still running. The handler's own cleanup releases it when work finishes.
        await asyncio.shield(task)
