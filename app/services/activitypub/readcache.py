"""A short-lived cache in front of the ActivityPub READ routes -- what a relay subscription costs.

Measured 2026-09-24, minutes after subscribing to relay.fedi.agency: ~37,000 fetches in three minutes
(19,317 objects, 7,443 actors, 3,682 WebFinger, plus featured / followers / following / outbox), each
recent post fetched ~1,200 times -- every instance subscribed to the relay dereferencing the post it
was shown, its author and the author's collections. Each fetch REBUILT the document from the relay
(several reads, mention resolution), on the one app worker, and the web client stopped loading.

Every one of those answers is the same for every fetcher, so it is built once and served from memory:

  * only GET, only the public read paths below, only 200/404/410 answers with no Set-Cookie;
  * keyed by host + path + query + whether an HTML page or an ActivityPub document was asked for
    (the actor route answers a browser with a redirect and a server with JSON);
  * 5 minutes for a document, 1 minute for a not-found / gone, at most MAX_ENTRIES;
  * SINGLE-FLIGHT: requests for a key that is being built WAIT for that build instead of starting their
    own -- under a relay's burst a cold cache would otherwise still mean one build per request.

Staleness is bounded and harmless for these documents: a deleted post answers 410 within five
minutes (the Delete itself was already DELIVERED to every server that holds it), a profile edit is
delivered as Update(Person), and counts are labels.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

TTL_OK = 300.0
TTL_MISSING = 60.0
MAX_ENTRIES = 5000
MAX_BODY = 1024 * 1024
_PREFIXES = ("/ap/objects/", "/ap/users/", "/ap/actor", "/.well-known/webfinger", "/.well-known/host-meta",
             "/.well-known/nodeinfo", "/nodeinfo/")

_cache: "OrderedDict[str, tuple]" = OrderedDict()     # key -> (expires, status, headers, body)
_building: dict = {}                                   # key -> asyncio.Future of the entry


def _wants_html(headers: dict) -> bool:
    accept = headers.get("accept", "").lower()
    return "text/html" in accept and "activity+json" not in accept and "ld+json" not in accept


def clear() -> None:
    _cache.clear()
    _building.clear()


class ActivityPubReadCache:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "GET" \
                or not scope.get("path", "").startswith(_PREFIXES):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        if "origin" in headers or "authorization" in headers or "cookie" in headers:
            # A browser (CORS headers vary by Origin) or a signed-in request: never shared.
            return await self.app(scope, receive, send)
        key = "|".join((headers.get("host", ""), scope.get("path", ""), scope.get("query_string", b"").decode("latin-1"),
                        "html" if _wants_html(headers) else "ap"))
        now = time.monotonic()
        hit = _cache.get(key)
        if hit and hit[0] > now:
            _cache.move_to_end(key)
            return await _replay(hit, send)
        pending = _building.get(key)
        if pending is not None:
            try:
                entry = await asyncio.shield(pending)
            except Exception:
                entry = None
            if entry is not None:
                return await _replay(entry, send)
            return await self.app(scope, receive, send)       # the build it waited on failed: do its own
        fut = asyncio.get_running_loop().create_future()
        _building[key] = fut
        captured = {"status": 0, "headers": [], "body": bytearray(), "cacheable": True}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = list(message.get("headers") or [])
                if any(k.lower() == b"set-cookie" for k, _ in captured["headers"]):
                    captured["cacheable"] = False
            elif message["type"] == "http.response.body":
                captured["body"].extend(message.get("body", b""))
                if len(captured["body"]) > MAX_BODY:
                    captured["cacheable"] = False
            await send(message)

        entry = None
        try:
            await self.app(scope, receive, capture)
            status = captured["status"]
            if captured["cacheable"] and status in (200, 404, 410):
                ttl = TTL_OK if status == 200 else TTL_MISSING
                entry = (time.monotonic() + ttl, status, captured["headers"], bytes(captured["body"]))
                _cache[key] = entry
                _cache.move_to_end(key)
                while len(_cache) > MAX_ENTRIES:
                    _cache.popitem(last=False)
        finally:
            _building.pop(key, None)
            if not fut.done():
                fut.set_result(entry)


async def _replay(entry, send):
    _expires, status, headers, body = entry
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
