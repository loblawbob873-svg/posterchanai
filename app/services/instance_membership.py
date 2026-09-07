"""App membership: an assigned instance name AND that exact signed profile address.

No account-role exemption. Profiles are independently verified from the local relay
and, when needed, bounded administrator-configured upstreams. Client relay URLs
and unsigned profile claims never select an authorization source.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.services import settings_store
from app.services.nostr import event, nostr_service
from app.services.nostr_relay.thread import _parse_nip05

KEY = re.compile(r'^[0-9a-f]{64}$')
MAX_CACHE = 1024
MAX_PENDING = 32
QUERY_TIMEOUT = 4


def _configuration():
    if not settings_store.is_hydrated():
        raise HTTPException(503, 'Instance membership settings are not loaded yet')
    return (settings_store.get('nostr_relay_nip05_names', '') or '',
            settings_store.get('nostr_relay_nip05_domain', '') or '',
            settings_store.get('site_url', '') or '',
            settings_store.get('nostr_relay_port', '3052') or '3052',
            settings_store.get('nostr_relay_upstream_relays', '') or '')


def _domain(value):
    value = value.strip().lstrip('@').lower().rstrip('.')
    if not value or any(c in value for c in '/:@?#'):
        return ''
    try:
        return value.encode('idna').decode('ascii')
    except UnicodeError:
        return ''


def _address(value):
    if not isinstance(value, str) or value.strip().count('@') != 1:
        return ''
    name, host = value.strip().split('@')
    domain = _domain(host)
    return name + '@' + domain if name and domain else ''


async def _read_profile(ws, pk):
    sub = 'member-' + secrets.token_hex(6)
    rows = []
    await ws.send(json.dumps(['REQ', sub, {'kinds': [0], 'authors': [pk], 'limit': 32}]))
    while True:
        message = json.loads(await ws.recv())
        if not isinstance(message, list) or len(message) < 2 or message[1] != sub:
            continue
        if message[0] == 'EOSE':
            return rows
        if message[0] == 'CLOSED':
            raise RuntimeError('Profile subscription refused')
        if message[0] == 'EVENT' and len(message) == 3:
            rows.append(message[2])
            if len(rows) > 32:
                raise RuntimeError('Profile response exceeded limit')


async def _query_profile(pk, port):
    """CLOSED/timeout is unavailable, never evidence of a missing profile."""
    import websockets
    async with websockets.connect(f'ws://127.0.0.1:{int(port)}/relay',
                                  open_timeout=QUERY_TIMEOUT, close_timeout=1,
                                  max_size=262144, proxy=None) as ws:
        return await _read_profile(ws, pk)


async def _open_bounded_proxy(relay, base, timeout, kw):
    """Close an HTTP CONNECT socket on cancellation, including a late proxy response.

    websockets' HTTP proxy handshake leaves the transport open when its response future is
    cancelled. python-socks already supplies the application's proxy transport and closes
    its socket for CancelledError as well as ordinary connection failures.
    """
    import websockets
    from python_socks.async_.asyncio import Proxy
    target = urlsplit(relay)
    sock = await Proxy.from_url(base['proxy']).connect(
        dest_host=target.hostname, dest_port=target.port or (443 if target.scheme == 'wss' else 80),
        timeout=timeout)
    try:
        return await websockets.connect(relay, sock=sock, proxy=None, open_timeout=timeout, **kw)
    except BaseException:
        sock.close()
        raise


@asynccontextmanager
async def _connect_profile(uri, budget):
    import websockets
    timeout = min(1, budget / 3)
    options = {'max_size':262144, 'close_timeout':1, 'user_agent_header':'PosterChan/Server'}
    base = nostr_service.relay._conn_kw(uri, False)
    if not str(base.get('proxy', '')).startswith('http://'):
        async with nostr_service.relay._connect(uri, False, max_size=262144, close_timeout=1) as ws:
            yield ws
        return
    try:
        ws = await _open_bounded_proxy(uri, base, timeout, options)
    except Exception:
        # Same proxy-first/direct-fallback policy as the ordinary relay transport, bounded
        # within this request's deadline instead of its default eight-second handshake.
        ws = await websockets.connect(uri, proxy=None, open_timeout=timeout, **options)
    try:
        yield ws
    finally:
        await ws.close()


async def _query_upstreams(pk, relays, budget):
    async def one(uri):
        # Admin-configured endpoints only; preserve the application's proxy transport.
        async with _connect_profile(uri, budget) as ws:
            return await _read_profile(ws, pk)
    tasks = [asyncio.create_task(one(uri)) for uri in relays]
    try:
        done, _ = await asyncio.wait(tasks, timeout=budget)
        complete = [task.result() for task in done if not task.cancelled() and task.exception() is None]
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if not complete:
        raise RuntimeError('Configured profile relays are unavailable')
    return [row for rows in complete for row in rows]


def _upstreams(config):
    raw = config[4] if len(config) > 4 else ''
    relays = []
    for uri in nostr_service.relay.normalize_relays(raw):
        parsed = urlsplit(uri)
        if parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment:
            relays.append(uri)
        if len(relays) == 3:
            break
    if raw.strip() and not relays:
        raise ValueError('No valid configured profile relays')
    return relays


def _profile_address(row):
    if not row:
        return ''
    try:
        profile = json.loads(row['content'])
    except (ValueError, TypeError):
        return ''
    return _address(profile.get('nip05')) if isinstance(profile, dict) else ''


def _latest(rows, pk, wallclock):
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('Invalid profile response')
    valid = []
    for row in rows:
        if (isinstance(row, dict) and row.get('pubkey') == pk and row.get('kind') == 0
                and type(row.get('created_at')) is int and 0 <= row['created_at'] <= wallclock + 300
                and isinstance(row.get('id'), str) and KEY.fullmatch(row['id'])
                and isinstance(row.get('content'), str) and len(row['content']) <= 131072
                and event.verify_event(row)):
            valid.append(row)
    return min(valid, key=lambda e: (-e['created_at'], e['id'])) if valid else None


class MembershipChecker:
    def __init__(self, *, query=None, configuration=None, clock=None, wallclock=None, upstream_query=None):
        self.query = query or _query_profile
        self.upstream_query = upstream_query or _query_upstreams
        self.configuration = configuration or _configuration
        self.clock = clock or time.monotonic
        self.wallclock = wallclock or time.time
        self.cache = OrderedDict()
        self.watermarks = OrderedDict()
        self.pending = {}
        self.jobs = set()
        self.config = None

    async def status(self, pubkey, *, force=False):
        pk = nostr_service.to_pubkey_hex(pubkey or '')
        if not pk:
            raise HTTPException(403, 'A signed-in Nostr account is required')
        pk = pk.lower()
        config = tuple(self.configuration())
        if config != self.config:
            self.cache.clear()
            self.config = config
        names, _ = _parse_nip05(config[0], '')
        domain = _domain(config[1]) or _domain(urlsplit(config[2]).hostname or '')
        aliases = sorted(name for name, owner in names.items() if owner.lower() == pk)
        base = {'pubkey': pk, 'qualified': False, 'address': '', 'domain': domain,
                'profile_address': '', 'reason': 'unregistered'}
        if not aliases:
            return base
        if not domain:
            raise HTTPException(503, 'Instance NIP-05 domain is not configured')
        base['address'] = aliases[0] + '@' + domain
        key = (pk, config)
        cached = self.cache.get(key)
        pending = self.pending.get(key)
        if force and cached:
            self.cache[key] = (0, cached[1], cached[2])
        if not force and not pending and cached and cached[0] > self.clock():
            self.cache.move_to_end(key)
            return dict(cached[1])
        if pending and (not force or pending[1]):
            job = pending[0]
        else:
            if len(self.jobs) >= MAX_PENDING:
                raise HTTPException(503, 'Instance membership check is busy')
            ticket = object()
            job = asyncio.create_task(self._check(key, aliases, base, cached, ticket, force))
            self.pending[key] = (job, bool(force), ticket)
            self.jobs.add(job)
            def finished(done):
                self.jobs.discard(done)
                if self.pending.get(key, (None,))[0] is done:
                    self.pending.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Consume errors even when all waiting clients disconnected.
            job.add_done_callback(finished)
        # A disconnected caller must not cancel a check shared with other requests.
        result = await asyncio.shield(job)
        if tuple(self.configuration()) != config:
            raise HTTPException(503, 'Instance membership configuration changed; retry')
        return dict(result)

    async def _check(self, key, aliases, base, cached, ticket, force):
        pk, config = key
        previous = self.watermarks.get(pk) or (cached[2] if cached else None)
        expected = {name + '@' + base['domain'] for name in aliases}
        started = asyncio.get_running_loop().time()
        try:
            local_failed = False
            try:
                rows = await asyncio.wait_for(self.query(pk, config[3]), QUERY_TIMEOUT)
                newest = await asyncio.to_thread(_latest, rows, pk, self.wallclock())
            except Exception:
                local_failed, rows, newest = True, [], None
            relays = _upstreams(config)
            if local_failed and not relays:
                raise RuntimeError('Local profile relay unavailable')
            local_mark = (newest['created_at'], newest['id']) if newest else None
            stale = previous and (not local_mark or local_mark[0] < previous[0] or
                                  (local_mark[0] == previous[0] and local_mark[1] > previous[1]))
            if relays and (force or stale or _profile_address(newest) not in expected):
                remaining = QUERY_TIMEOUT - (asyncio.get_running_loop().time() - started)
                if remaining <= 0:
                    raise TimeoutError('Profile lookup deadline reached')
                external = await asyncio.wait_for(self.upstream_query(pk, relays, remaining), remaining + 1.25)
                if not isinstance(external, list) or len(external) > 96:
                    raise ValueError('Invalid upstream profile response')
                newest = await asyncio.to_thread(_latest, rows + external, pk, self.wallclock())
            if local_failed and not newest:
                raise RuntimeError('No verified profile available during local relay outage')
        except Exception as exc:
            raise HTTPException(503, 'Instance profile verification is temporarily unavailable') from exc
        watermark = (newest['created_at'], newest['id']) if newest else None
        if previous and (not watermark or watermark[0] < previous[0] or
                         (watermark[0] == previous[0] and watermark[1] > previous[1])):
            raise HTTPException(503, 'Latest instance profile is temporarily unavailable')
        result = dict(base, reason='profile_missing')
        if newest:
            address = _profile_address(newest)
            result.update(profile_address=address, reason='profile_mismatch')
            if address in expected:
                result.update(qualified=True, address=address, reason='qualified')
        if (tuple(self.configuration()) != config or
                self.pending.get(key, (None, None, None))[2] is not ticket):
            raise HTTPException(503, 'Instance membership check superseded; retry')
        if watermark:
            self.watermarks[pk] = watermark
            self.watermarks.move_to_end(pk)
            while len(self.watermarks) > MAX_CACHE:
                self.watermarks.popitem(last=False)
        self.cache[key] = (self.clock() + (30 if result['qualified'] else 5), result, watermark)
        self.cache.move_to_end(key)
        while len(self.cache) > MAX_CACHE:
            self.cache.popitem(last=False)
        return result

    async def require_pubkey(self, pubkey):
        result = await self.status(pubkey)
        if not result['qualified']:
            raise HTTPException(403, 'Set your approved instance NIP-05 address in your profile to use this app')
        return result

    async def require_user(self, user):
        await self.require_pubkey(getattr(user, 'nostr_npub', '') or '')
        return user


_checker = MembershipChecker()


async def status(pubkey, *, force=False):
    return await _checker.status(pubkey, force=force)


async def require_pubkey(pubkey):
    return await _checker.require_pubkey(pubkey)


async def require_user(user):
    return await _checker.require_user(user)
