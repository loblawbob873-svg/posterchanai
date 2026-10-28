"""THE USER AGENT THIS SERVER SENDS TO OTHER RELAYS, AND THE RELAYS THAT REFUSE IT.

Outbound relay sockets say `PosterChan/Server`: a PosterChan relay running "PosterChan clients only" admits a peer
server by it (nostr_relay/server.py `_posterchan_client_allowed`). But a relay may refuse exactly that string, and
one does: wss://jskitty.com/nostr -- a CORD relay that carries more Concord streams than any other we mirror --
answers HTTP 403 to `PosterChan/Server` and 101 to the library default, a browser's, or none (measured 2026-10-27).
Every server-side socket sent it, so this node's Concord mirror and tunnel had read nothing from that relay: whole
conversations were missing from the phone ("that entire conversation is missing on mobile") while a client that
reached the relay itself showed them.

So a 401/403 to a socket that carried the header is retried at once WITHOUT it, and the host is remembered for
REFUSED_FOR so later sockets skip it. A caller that passes `user_agent_header` itself is left alone.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

UA = "PosterChan/Server"

# RELAYS THIS NODE NEVER DIALS, from any process, for any feature ("we should never connect to the damus relay, it's
# super rate-limited", 2026-10-27). Every outbound relay socket comes through connect() below, so this is the one
# place the rule can live; the client's twin is relay.js _BLOCKED_RELAY_HOST. A community may still LIST such a relay
# (its list is not ours to edit) -- we just never open a socket to it.
NEVER_DIAL = frozenset({"relay.damus.io"})


class NeverDialled(ConnectionRefusedError):
    """Raised instead of opening a socket to a NEVER_DIAL host."""


def never_dial(url: str) -> bool:
    return _host(url) in NEVER_DIAL


REFUSED_FOR = 6 * 3600
_refused: dict[str, float] = {}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or url).lower()
    except ValueError:
        return url


def header(url: str) -> str | None:
    """`PosterChan/Server`, or None (no User-Agent at all) for a host that refused it lately."""
    return None if _refused.get(_host(url), 0) > time.monotonic() else UA


def _status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) or getattr(exc, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def refused_by(url: str, exc: BaseException) -> bool:
    """True, and remembered, when `exc` is the relay refusing the handshake (401/403)."""
    if _status(exc) in (401, 403):
        _refused[_host(url)] = time.monotonic() + REFUSED_FOR
        if len(_refused) > 4096:
            now = time.monotonic()
            for h in [h for h, t in _refused.items() if t <= now]:
                _refused.pop(h, None)
        return True
    return False


class connect:
    """`websockets.connect(url, **kw)` with the User-Agent rule above. Usable both ways websockets' own is:
    `ws = await connect(...)` and `async with connect(...) as ws` (or `cm.__aenter__()` by hand)."""

    def __init__(self, url: str, **kw):
        self.url, self.kw, self._cm = url, kw, None

    async def _one(self, entered, **extra):
        import websockets
        cm = websockets.connect(self.url, **self.kw, **extra)
        if entered and hasattr(cm, "__aenter__"):
            self._cm = cm
            return await cm.__aenter__()
        return await cm

    async def _open(self, entered):
        if never_dial(self.url):
            raise NeverDialled(f"{_host(self.url)} is never dialled from this node")
        if "user_agent_header" in self.kw:
            return await self._one(entered)
        ua = header(self.url)
        try:
            return await self._one(entered, user_agent_header=ua)
        except Exception as e:
            if ua and refused_by(self.url, e):
                return await self._one(entered, user_agent_header=None)
            raise

    async def __aenter__(self):
        return await self._open(True)

    async def __aexit__(self, *exc):
        if self._cm is not None:
            return await self._cm.__aexit__(*exc)
        return False

    def __await__(self):
        return self._open(False).__await__()
