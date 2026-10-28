"""CONCORD ON THIS NODE'S RELAY — the server half (app/services/nostr_relay/concord_mirror.py).

The owner's request: "for telegram, we store everything on the relay and it works great. any way to make our
concord implementation do that also so each client don't have to communicate with other concord relays?"

Everything here runs the SHIPPED RelayServer (its write and read paths, its OK/CLOSED answers) and the shipped
ConcordMirror against real websocket relays standing in for a community's upstream relays, over real sockets.
Only the event store is in memory. Each rule below was run against the tree without the mirror and failed:
there was no registration (kind 27059 was an ordinary ephemeral event, OK'd and fanned out to every
subscriber), nothing ever connected upstream, and a REQ for a stream was answered EOSE from whatever the store
happened to hold.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time

import pytest
import websockets

from app.services.nostr.event import build_event
from app.services.nostr_relay import concord_mirror as cm
from app.services.nostr_relay.server import RelayServer
from tests.relay_fake import _matches, _order


# --- the pieces -------------------------------------------------------------------------------------------

def key():
    return secrets.token_bytes(32)


def pub(sk):
    from app.services.nostr import bip340
    return bip340.pubkey_from_seckey(sk).hex()


def wrap(sk, created_at=None, kind=1059):
    """An opaque Concord envelope as a community stream key signs it: base64-looking ciphertext, a random
    cover `p`. Nothing here (or on the relay) can open it."""
    return build_event(sk, kind, secrets.token_urlsafe(180),
                       tags=[["p", secrets.token_hex(32)]], created_at=created_at)


class MemStore:
    """The relay store's interface, in memory. Stores the dict it is handed, as handed."""

    def __init__(self):
        self.events: dict = {}
        self.kv: dict = {}
        self.deleted: list = []

    async def query(self, filters, hard_cap=5000):
        seen = {}
        for f in filters:
            lim = max(1, min(int(f.get("limit") or 500), 5000))
            for e in _order([e for e in self.events.values() if _matches(e, f)])[:lim]:
                seen[e["id"]] = e
        return _order(seen.values())[:hard_cap]

    async def add_event(self, ev, origin="wot"):
        self.events.setdefault(ev["id"], ev)
        return True

    async def has_event(self, eid):
        return eid in self.events

    async def kv_get(self, k):
        return self.kv.get(k)

    async def kv_set(self, k, v):
        self.kv[k] = v

    async def delete_pubkeys(self, pks, spare_preserved=True):
        pks = set(pks)
        gone = [i for i, e in self.events.items() if e["pubkey"] in pks]
        for i in gone:
            del self.events[i]
        self.deleted.extend(pks)
        return len(gone)

    async def count_filtered(self, filters, protect_nip78=False):
        return sum(1 for e in self.events.values() for f in filters if _matches(e, f))


class Gate:
    def __init__(self, operators=()):
        self.operators = set(operators)

    def is_member(self, _pk): return True
    def is_operator(self, pk): return pk in self.operators
    def is_blocked(self, _pk): return False
    def is_puppet_event(self, _ev): return False
    def is_bridged(self, _pk): return False


class Upstream:
    """A community relay: serves REQs (limit/until honoured, newest first), stores EVENTs it accepts, and
    streams new matching events to open subscriptions. `refuse` answers every REQ with CLOSED."""

    def __init__(self, events=(), *, refuse="", accept=True):
        self.events = list(events)
        self.refuse = refuse
        self.accept = accept
        self.subs: dict = {}
        self.writes: list = []
        self.reqs: list = []

    async def start(self):
        self.srv = await websockets.serve(self._h, "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        return self

    async def close(self):
        self.srv.close()
        await self.srv.wait_closed()

    async def _h(self, ws):
        mine = self.subs.setdefault(ws, {})
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == "REQ":
                    sid, filters = msg[1], msg[2:]
                    self.reqs.append(filters)
                    if self.refuse:
                        await ws.send(json.dumps(["CLOSED", sid, self.refuse]))
                        continue
                    seen = {}
                    for f in filters:
                        lim = int(f.get("limit") or 10_000)
                        for e in _order([e for e in self.events if _matches(e, f)])[:lim]:
                            seen[e["id"]] = e
                    for e in _order(seen.values()):
                        await ws.send(json.dumps(["EVENT", sid, e]))
                    await ws.send(json.dumps(["EOSE", sid]))
                    mine[sid] = filters
                elif msg[0] == "CLOSE":
                    mine.pop(msg[1], None)
                elif msg[0] == "EVENT":
                    ev = msg[1]
                    if not self.accept:
                        await ws.send(json.dumps(["OK", ev["id"], False, "blocked: no"]))
                        continue
                    self.writes.append(ev)
                    await ws.send(json.dumps(["OK", ev["id"], True, ""]))
                    await self.inject(ev, store=True)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.subs.pop(ws, None)

    async def inject(self, ev, store=True):
        if store and not any(e["id"] == ev["id"] for e in self.events):
            self.events.append(ev)
        for ws, subs in list(self.subs.items()):
            for sid, filters in list(subs.items()):
                if any(_matches(ev, {k: v for k, v in f.items() if k not in ("since", "until", "limit")})
                       for f in filters):
                    try:
                        await ws.send(json.dumps(["EVENT", sid, ev]))
                    except Exception:
                        pass


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"ws://127.0.0.1:{port}"


class Node:
    """This node: the shipped RelayServer + ConcordMirror over a memory store, listening on a real socket."""

    def __init__(self, operators, cfg=None, **mirror_kw):
        self.store = MemStore()
        self.cfg = {"wot_enabled": False, "concord_mirror": True, **(cfg or {})}
        self.server = RelayServer(self.store, Gate(operators), self.cfg)
        mirror_kw.setdefault("url_ok", lambda u, c: "")
        mirror_kw.setdefault("open_ws", lambda url: websockets.connect(url, open_timeout=3))
        self.mirror = cm.ConcordMirror(self.store, self.server, self.cfg, **mirror_kw)
        self.server.mirror = self.mirror

    async def start(self):
        self.ws = await websockets.serve(self.server.handle, "127.0.0.1", 0,
                                         process_request=self.server.process_request)
        self.url = f"ws://127.0.0.1:{self.ws.sockets[0].getsockname()[1]}"
        await self.mirror.start()
        return self

    async def close(self):
        await self.mirror.stop()
        self.ws.close()
        await self.ws.wait_closed()

    async def send(self, msg, want):
        """One message on a fresh client socket; collect replies until `want(msgs)`."""
        async with websockets.connect(self.url) as c:
            await c.send(json.dumps(msg))
            got = []
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(c.recv(), timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    break
                m = json.loads(raw)
                if m[0] in ("AUTH", "NOTICE"):
                    continue
                got.append(m)
                if want(got):
                    break
            return got

    async def req(self, filters, prefix="s"):
        sid = prefix + secrets.token_hex(4)
        got = await self.send(["REQ", sid, *filters], lambda g: g[-1][0] in ("EOSE", "CLOSED"))
        return [m[2] for m in got if m[0] == "EVENT"], got[-1] if got else None

    async def publish(self, ev):
        got = await self.send(["EVENT", ev], lambda g: any(m[0] == "OK" for m in g))
        return next(m for m in got if m[0] == "OK")


def registration(member_sk, relays, authors, *, group=None, op="mirror", created_at=None):
    body = {"v": 1, "op": op, "group": group or ("ab" * 32)}
    if op == "mirror":
        body.update(relays=relays, authors=authors)
    return build_event(member_sk, cm.KIND_REGISTER, json.dumps(body), tags=[["t", "concord-mirror"]],
                       created_at=created_at)


async def until(pred, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


# --- the tests ----------------------------------------------------------------------------------------------

def test_history_on_the_upstream_relay_is_mirrored_into_this_relay(monkeypatch):
    """A community's existing history, paged in from its relay, is served by THIS relay — every wrap, as is."""
    monkeypatch.setattr(cm, "HISTORY_PAGE", 4)          # force paging back through history
    stream, member = key(), key()
    now = int(time.time())
    history = [wrap(stream, created_at=now - 1000 + i * 7) for i in range(11)]

    async def go():
        up = await Upstream(history).start()
        node = await Node({pub(member)}).start()
        try:
            ok = await node.publish(registration(member, [up.url], [pub(stream)]))
            assert ok[2] is True, ok
            assert await until(lambda: node.mirror.author_ready(pub(stream)))
            got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)], "limit": 100}])
            assert last[0] == "EOSE", last
            assert sorted(e["id"] for e in got) == sorted(e["id"] for e in history)
            # paged: more than one history REQ carried an `until`
            assert sum(1 for fs in up.reqs for f in fs if "until" in f) >= 2
        finally:
            await node.close()
            await up.close()
    run(go())


def test_a_post_made_here_reaches_the_upstream_and_one_posted_upstream_appears_here():
    stream, member = key(), key()

    async def go():
        up = await Upstream().start()
        node = await Node({pub(member)}).start()
        try:
            assert (await node.publish(registration(member, [up.url], [pub(stream)])))[2] is True
            assert await until(lambda: node.mirror.author_ready(pub(stream)))
            # here -> there: the OK waits for the upstream, and says it took it
            mine = wrap(stream)
            ok = await node.publish(mine)
            assert ok[2] is True, ok
            assert [e["id"] for e in up.writes] == [mine["id"]]
            assert up.writes[0] == mine
            # there -> here: live, into the store and onto an open subscription on this relay
            async with websockets.connect(node.url) as c:
                await c.send(json.dumps(["REQ", "live", {"kinds": [1059], "authors": [pub(stream)]}]))
                theirs = wrap(stream)
                seen = []
                deadline = time.monotonic() + 10
                injected = False
                while time.monotonic() < deadline:
                    m = json.loads(await asyncio.wait_for(c.recv(), timeout=10))
                    if m[0] == "EOSE" and not injected:
                        injected = True
                        await up.inject(theirs)
                    if m[0] == "EVENT":
                        seen.append(m[2]["id"])
                        if theirs["id"] in seen:
                            break
                assert theirs["id"] in seen
            assert node.store.events[theirs["id"]] == theirs
        finally:
            await node.close()
            await up.close()
    run(go())


def test_an_upstream_that_cannot_be_reached_is_never_no_messages():
    """Unreachable, refusing (auth-required) or not yet caught up: a REQ is CLOSED `unavailable:`, never an
    EOSE over an empty store — and a post the upstream did not take is OK false, so the client delivers it."""
    stream, stream2, member = key(), key(), key()

    async def go():
        refusing = await Upstream([wrap(stream2)], refuse="auth-required: we want AUTH").start()
        node = await Node({pub(member)}).start()
        try:
            assert (await node.publish(registration(member, [closed_port_url()], [pub(stream)],
                                                    group="cd" * 32)))[2] is True
            assert (await node.publish(registration(member, [refusing.url], [pub(stream2)],
                                                    group="ef" * 32)))[2] is True
            await asyncio.sleep(1.5)
            for s in (stream, stream2):
                got, last = await node.req([{"kinds": [1059], "authors": [pub(s)]}])
                assert got == [] and last[0] == "CLOSED" and last[2].startswith("unavailable:"), last
                assert not node.mirror.author_ready(pub(s))
            ok = await node.publish(wrap(stream))
            assert ok[2] is False and ok[3].startswith("unavailable:"), ok
            # a stream nobody registered is the store's business, as it always was
            other = key()
            node.store.events.update({e["id"]: e for e in [wrap(other)]})
            got, last = await node.req([{"kinds": [1059], "authors": [pub(other)]}])
            assert last[0] == "EOSE" and len(got) == 1
        finally:
            await node.close()
            await refusing.close()
    run(go())


def test_the_server_stores_only_opaque_wraps_and_never_decrypts(monkeypatch):
    from app.services.nostr import nip04, nip44
    calls = []
    for mod in (nip04, nip44):
        for name in dir(mod):
            if "decrypt" in name.lower() and callable(getattr(mod, name)):
                monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: calls.append(_n) or (_ for _ in ()).throw(AssertionError(_n)))
    stream, member = key(), key()
    history = [wrap(stream) for _ in range(5)]
    raw = {e["id"]: json.dumps(e, sort_keys=True) for e in history}

    async def go():
        up = await Upstream(history).start()
        node = await Node({pub(member)}).start()
        try:
            await node.publish(registration(member, [up.url], [pub(stream)]))
            assert await until(lambda: len(node.store.events) == 5)
            for eid, ev in node.store.events.items():
                assert json.dumps(ev, sort_keys=True) == raw[eid]          # byte-identical
            got, _ = await node.req([{"kinds": [1059], "authors": [pub(stream)]}])
            assert {json.dumps(e, sort_keys=True) for e in got} == set(raw.values())
        finally:
            await node.close()
            await up.close()
    run(go())
    assert calls == []
    # and the module does not even import a decryption routine
    src = open(cm.__file__).read()
    assert "nip44" not in src and "nip04" not in src and "decrypt(" not in src


def test_leaving_stops_the_mirror():
    stream, member = key(), key()

    async def go():
        up = await Upstream([wrap(stream)]).start()
        node = await Node({pub(member)}).start()
        try:
            group = "12" * 32
            await node.publish(registration(member, [up.url], [pub(stream)], group=group))
            assert await until(lambda: node.mirror.author_ready(pub(stream)))
            assert await until(lambda: len(node.store.events) == 1)
            ok = await node.publish(registration(member, [], [], group=group, op="leave"))
            assert ok[2] is True, ok
            assert node.mirror.links == {}
            assert await until(lambda: not up.subs or all(not s for s in up.subs.values()))
            assert node.store.events == {}                      # the copies went with it
            await up.inject(wrap(stream))                       # nothing listens any more
            await asyncio.sleep(0.5)
            assert node.store.events == {}
            assert not node.mirror.mirrors(pub(stream))
            await node.mirror._save_now()                       # (saves are debounced)
            assert json.loads(node.store.kv[cm.KV_KEY])["regs"] == {}
        finally:
            await node.close()
            await up.close()
    run(go())


def test_a_registration_nobody_refreshed_expires(monkeypatch):
    stream, member = key(), key()

    async def go():
        up = await Upstream([wrap(stream)]).start()
        node = await Node({pub(member)}).start()
        try:
            await node.publish(registration(member, [up.url], [pub(stream)]))
            assert await until(lambda: node.mirror.author_ready(pub(stream)))
            for groups in node.mirror.regs.values():
                for reg in groups.values():
                    reg["at"] -= cm.TTL_SECONDS + 10
            assert await node.mirror._expire()
            await node.mirror._reconcile()
            assert node.mirror.links == {} and node.store.events == {}
        finally:
            await node.close()
            await up.close()
    run(go())


def test_the_caps_hold(monkeypatch):
    monkeypatch.setattr(cm, "MAX_EVENTS_PER_COMMUNITY", 5)
    monkeypatch.setattr(cm, "MAX_REGISTRATIONS_PER_MEMBER", 2)
    stream, member, stranger = key(), key(), key()
    history = [wrap(stream, created_at=int(time.time()) - i) for i in range(12)]

    async def go():
        up = await Upstream(history).start()
        node = await Node({pub(member)}).start()
        try:
            # a stranger cannot make this node dial anywhere
            ok = await node.publish(registration(stranger, [up.url], [pub(stream)]))
            assert ok[2] is False and ok[3].startswith("restricted:"), ok
            # too many streams in one community
            many = [secrets.token_hex(32) for _ in range(cm.MAX_AUTHORS_PER_REGISTRATION + 1)]
            ok = await node.publish(registration(member, [up.url], many))
            assert ok[2] is False and ok[3].startswith("blocked:"), ok
            # a stale or future registration
            ok = await node.publish(registration(member, [up.url], [pub(stream)],
                                                 created_at=int(time.time()) - 3600))
            assert ok[2] is False and ok[3].startswith("invalid:"), ok
            # the per-community store budget: at most 5 of 12, and then the stream is NOT in step
            assert (await node.publish(registration(member, [up.url], [pub(stream)], group="01" * 32)))[2]
            await asyncio.sleep(1.5)
            assert len(node.store.events) == 5
            got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)]}])
            assert last[0] == "CLOSED" and last[2].startswith("unavailable:"), last
            # registrations per member
            assert (await node.publish(registration(member, [up.url], [secrets.token_hex(32)], group="02" * 32)))[2]
            ok = await node.publish(registration(member, [up.url], [secrets.token_hex(32)], group="03" * 32))
            assert ok[2] is False and "at most" in ok[3], ok
            # the total budget refuses new registrations once full
            monkeypatch.setattr(cm, "MAX_EVENTS_TOTAL", 5)
            monkeypatch.setattr(cm, "MAX_REGISTRATIONS_PER_MEMBER", 20)
            ok = await node.publish(registration(member, [up.url], [secrets.token_hex(32)], group="04" * 32))
            assert ok[2] is False and "full" in ok[3], ok
        finally:
            await node.close()
            await up.close()
    run(go())


def test_the_registration_is_never_fanned_out_or_stored():
    """It names relays and stream keys: no other socket may ever see it."""
    stream, member = key(), key()

    async def go():
        up = await Upstream().start()
        node = await Node({pub(member)}).start()
        try:
            async with websockets.connect(node.url) as watcher:
                await watcher.send(json.dumps(["REQ", "all", {"kinds": [cm.KIND_REGISTER]}, {}]))
                ev = registration(member, [up.url], [pub(stream)])
                await node.publish(ev)
                await asyncio.sleep(0.5)
                frames = []
                while True:
                    try:
                        frames.append(json.loads(await asyncio.wait_for(watcher.recv(), timeout=0.3)))
                    except asyncio.TimeoutError:
                        break
                assert not any(f[0] == "EVENT" and f[2]["id"] == ev["id"] for f in frames)
            assert ev["id"] not in node.store.events
        finally:
            await node.close()
            await up.close()
    run(go())


def test_relay_urls_are_judged_before_they_are_dialled():
    cfg = {"blocked_relays": {"evil.example"}, "posterchan_origins": ["https://poster.place"]}
    ok = cm.default_url_ok
    assert ok("ws://relay.damus.io", cfg)                       # plain ws
    assert ok("wss://relay.damus.io:7777", cfg)                 # odd port
    assert ok("wss://sub.evil.example", cfg)                    # blocklist (suffix)
    assert ok("wss://poster.place/relay", cfg)                  # this node
    assert ok("wss://localhost", cfg)                           # private
    assert ok("wss://127.0.0.1", cfg)


def test_the_mirror_is_off_without_a_proxy_and_says_so():
    member, stream = key(), key()

    async def go():
        node = Node({pub(member)}, cfg={"concord_mirror_proxy": ""}, open_ws=None)
        node.mirror._open_override = None
        await node.start()
        try:
            ok = await node.publish(registration(member, ["wss://relay.example"], [pub(stream)]))
            assert ok[2] is False and "proxy" in ok[3], ok
            node.cfg["concord_mirror"] = False
            ok = await node.publish(registration(member, ["wss://relay.example"], [pub(stream)]))
            assert ok[2] is False and "switched off" in ok[3], ok
            # the directory is refused, never answered from an empty store
            got, last = await node.req([{"kinds": [1], "search": "armada.buzz/invite", "limit": 100}])
            assert last[0] == "CLOSED" and last[2].startswith("unavailable:"), last
        finally:
            await node.close()
    run(go())


# --- the community directory ------------------------------------------------------------------------------

def test_the_directory_is_answered_from_this_nodes_copy(monkeypatch):
    poster = key()
    invites = [build_event(poster, 1, f"join us https://armada.buzz/invite/naddr1abc{i}#k{i}") for i in range(3)]
    noise = build_event(poster, 1, "armada.buzz/invite is mentioned but no link")

    async def go():
        up = await Upstream(invites + [noise]).start()
        monkeypatch.setattr(cm, "DIRECTORY_RELAYS", (up.url,))
        node = await Node(set()).start()
        try:
            got, last = await node.req([{"kinds": [1], "search": "armada.buzz/invite", "limit": 100},
                                        {"kinds": [1], "search": "poster.place/invite", "limit": 100}])
            assert last[0] == "EOSE", last
            assert sorted(e["id"] for e in got) == sorted(e["id"] for e in invites)
            assert node.store.events == {}                      # never in the timeline store
        finally:
            await node.close()
            await up.close()
    run(go())


def test_an_unreachable_directory_is_refused_not_empty(monkeypatch):
    async def go():
        monkeypatch.setattr(cm, "DIRECTORY_RELAYS", (closed_port_url(),))
        node = await Node(set()).start()
        try:
            got, last = await node.req([{"kinds": [1], "search": "armada.buzz/invite", "limit": 100}])
            assert got == [] and last[0] == "CLOSED" and last[2].startswith("unavailable:"), last
        finally:
            await node.close()
    run(go())


# --- the outbound path: the proxy's FALLBACK listener, and nothing else --------------------------------------

class RecordingProxy:
    """An HTTP CONNECT proxy that records each target and tunnels it to the loopback port named."""

    def __init__(self):
        self.targets = []

    async def start(self):
        self.srv = await asyncio.start_server(self._h, "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]
        return self

    async def _h(self, r, w):
        line = (await r.readline()).decode()
        while (await r.readline()) not in (b"\r\n", b""):
            pass
        method, target, _ = line.split()
        self.targets.append((method, target))
        port = int(target.rsplit(":", 1)[1])
        ur, uw = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await w.drain()

        async def pipe(a, b):
            try:
                while data := await a.read(65536):
                    b.write(data)
                    await b.drain()
            except Exception:
                pass
            finally:
                b.close()
        await asyncio.gather(pipe(r, uw), pipe(ur, w))

    async def close(self):
        self.srv.close()


def test_upstream_connections_go_through_the_fallback_proxy():
    stream, member = key(), key()
    history = [wrap(stream) for _ in range(3)]

    async def go():
        up = await Upstream(history).start()
        proxy = await RecordingProxy().start()
        node = Node({pub(member)}, cfg={"concord_mirror_proxy": f"http://127.0.0.1:{proxy.port}"}, open_ws=None)
        node.mirror._open_override = None
        await node.start()
        try:
            name = f"ws://upstream.test:{up.port}"            # a name only the proxy can reach
            assert (await node.publish(registration(member, [name], [pub(stream)])))[2] is True
            assert await until(lambda: len(node.store.events) == 3)
            assert ("CONNECT", f"upstream.test:{up.port}") in proxy.targets
            assert all(m == "CONNECT" for m, _t in proxy.targets)
        finally:
            await node.close()
            await proxy.close()
            await up.close()
    run(go())


class StubSocks:
    """A SOCKS5 server (no auth, CONNECT only) that tunnels to loopback — a Tor daemon that works."""

    def __init__(self):
        self.connects = []

    async def start(self):
        self.srv = await asyncio.start_server(self._h, "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]
        return self

    async def _h(self, r, w):
        await r.readexactly(3)
        w.write(b"\x05\x00")
        await w.drain()
        head = await r.readexactly(4)
        if head[3] == 3:
            n = (await r.readexactly(1))[0]
            host = (await r.readexactly(n)).decode()
        elif head[3] == 1:
            host = socket.inet_ntoa(await r.readexactly(4))
        else:
            host = str(await r.readexactly(16))
        port = int.from_bytes(await r.readexactly(2), "big")
        self.connects.append((host, port))
        ur, uw = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + port.to_bytes(2, "big"))
        await w.drain()

        async def pipe(a, b):
            try:
                while data := await a.read(65536):
                    b.write(data)
                    await b.drain()
            except Exception:
                pass
            finally:
                b.close()
        await asyncio.gather(pipe(r, uw), pipe(ur, w))


def test_the_fallback_listener_tries_tor1_then_tor2_then_direct(monkeypatch):
    """The listener the mirror dials is the real HttpToSocksProxy with `allow_direct` — its own rules decide."""
    from app.services import http_proxy_service as hp
    stream, member = key(), key()

    async def go():
        up = await Upstream([wrap(stream)]).start()
        tor2 = await StubSocks().start()
        dead = int(closed_port_url().rsplit(":", 1)[1])
        fallback = hp.HttpToSocksProxy(listen_host="127.0.0.1", listen_port=0, socks_host="127.0.0.1",
                                       socks_ports=[f"{dead}:tor1", f"{tor2.port}:tor2"], allow_direct=True)
        fallback._semaphore = asyncio.Semaphore(16)
        fsrv = await asyncio.start_server(fallback._handle_client, "127.0.0.1", 0)
        fport = fsrv.sockets[0].getsockname()[1]
        node = Node({pub(member)}, cfg={"concord_mirror_proxy": f"http://127.0.0.1:{fport}"}, open_ws=None)
        node.mirror._open_override = None
        await node.start()
        try:
            name = f"ws://upstream.test:{up.port}"
            assert (await node.publish(registration(member, [name], [pub(stream)])))[2] is True
            assert await until(lambda: len(node.store.events) == 1)
            assert ("upstream.test", up.port) in tor2.connects         # Tor 1 dead -> Tor 2 carried it

            # both Tor daemons down -> the fallback listener's DIRECT leg (its public-address check stubbed
            # to loopback, the only "public" host a test has)
            fallback.socks_targets = [("127.0.0.1", dead, "tor1"), ("127.0.0.1", dead, "tor2")]
            fallback._open = [0, 0]; fallback._bytes = [0.0, 0.0]; fallback._bytes_t = [0.0, 0.0]; fallback._picked = [0, 0]

            async def loopback(host, port):
                return "127.0.0.1"
            monkeypatch.setattr(hp, "_public_address", loopback)
            carried_by_tor2 = len(tor2.connects)
            stream2 = key()
            up.events.append(wrap(stream2))
            assert (await node.publish(registration(member, [name], [pub(stream2)], group="77" * 32)))[2] is True
            assert await until(lambda: any(e["pubkey"] == pub(stream2) for e in node.store.events.values()))
            assert len(tor2.connects) == carried_by_tor2                 # ...and Tor 2 did not carry it
        finally:
            await node.close()
            fsrv.close()
            await up.close()
    run(go())


def test_no_proxy_means_no_socket_at_all():
    """`_open` without the fallback proxy raises — there is no raw direct path to fall into."""
    m = cm.ConcordMirror(MemStore(), None, {"concord_mirror": True, "concord_mirror_proxy": ""})
    with pytest.raises(cm.Unavailable):
        m._open("wss://relay.example")
    assert not m.enabled()


def test_the_relay_thread_points_the_mirror_at_the_fallback_listener():
    from app.services.nostr_relay import thread

    def g(k, d=""):
        return {"proxy_fallback_port": "8119", "proxy_listen_port": "8118"}.get(k, d)

    def gi(k, d):
        try:
            return int(g(k, str(d)))
        except ValueError:
            return d

    def gb(k, d=False):
        return d
    assert thread._fallback_proxy_url(g, gi, lambda k, d=False: True) == "http://127.0.0.1:8119"
    assert thread._fallback_proxy_url(g, gi, lambda k, d=False: False) == ""


def test_a_room_one_of_whose_relays_refused_the_mirror_is_unavailable_not_partial():
    """A community on two relays where one REFUSES the mirror (relay.damus.io: auth-required, measured on
    poster.place 2026-10-13) and holds a message the other does not. Serving from this node's copy because the
    OTHER relay synced handed the member a room with that message missing -- "largely isolated to this room",
    while Vector, reading damus directly, showed it. A refused relay is "could not ask": the room is
    `unavailable:` and the client reads the community's relays itself."""
    stream, member = key(), key()
    only_there = wrap(stream)

    async def go():
        good = await Upstream([wrap(stream)]).start()
        refusing = await Upstream([only_there], refuse="auth-required: we want AUTH").start()
        node = await Node({pub(member)}).start()
        try:
            assert (await node.publish(registration(member, [good.url, refusing.url], [pub(stream)])))[2] is True
            await asyncio.sleep(2.0)
            got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)]}])
            assert not node.mirror.author_ready(pub(stream)), "a room missing a refused relay was called in step"
            assert last[0] == "CLOSED" and last[2].startswith("unavailable:"), (last, len(got))
        finally:
            await node.close()
            await good.close()
            await refusing.close()
    run(go())


def test_a_reader_that_covers_the_refused_relay_gets_this_nodes_copy_and_is_told_which_relay_to_read():
    """The other half of the rule above, measured 2026-10-14: two of the owner's communities list relay.damus.io
    beside six relays this node reads in full, and the strict `unavailable:` sent every device to ALL of them,
    through the tunnel, once a minute per stream (~64 tunnels a minute per device). The registration reply now
    NAMES the relays this node cannot read; a client that reads those itself marks its REQ (PARTIAL_SUB_PREFIX)
    and is served this node's copy. Everyone else still gets `unavailable:` -- the old, strict answer."""
    stream, member = key(), key()
    held_here, only_there = wrap(stream), wrap(stream)

    async def go():
        good = await Upstream([held_here]).start()
        refusing = await Upstream([only_there], refuse="auth-required: we want AUTH").start()
        node = await Node({pub(member)}).start()
        try:
            ok = await node.publish(registration(member, [good.url, refusing.url], [pub(stream)]))
            assert ok[2] is True, ok
            assert "read directly:" in ok[3], ("the reply does not name the relay this node cannot read", ok)
            named = ok[3].split("read directly:", 1)[1].split()
            assert named == [refusing.url], (named, ok)
            assert await until(lambda: any(e["id"] == held_here["id"] for e in node.store.events.values()))
            got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)]}], prefix="pcmp")
            assert last[0] == "EOSE", ("a reader covering the refused relay was still refused", last)
            assert [e["id"] for e in got] == [held_here["id"]], got
            _got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)]}])
            assert last[0] == "CLOSED" and last[2].startswith("unavailable:"), ("strict readers lost the old answer", last)
        finally:
            await node.close()
            await good.close()
            await refusing.close()
    run(go())


def test_the_mirror_walks_every_tor_then_direct(monkeypatch):
    """"tor1 tor2 then default" (2026-10-27): the mirror dials each Tor daemon in the proxy's order, then direct, and
    the circuit that worked goes first next time -- never direct first (asia.vectorapp.io answers ONE of four exits,
    and the old direct-first memory sent this node's own address to a relay that now 403s it)."""
    import websockets
    from app.services import concord_tunnel as tun
    from app.services.nostr_relay import concord_mirror as cm
    T1, T2 = "socks5h://127.0.0.1:9052", "socks5h://127.0.0.1:9062"
    monkeypatch.setattr(tun, "_good", {})
    seen, works = [], {T2}

    class _CM:
        def __init__(self, proxy):
            self.proxy = proxy

        async def __aenter__(self):
            if self.proxy not in works:
                raise TimeoutError("timed out during opening handshake")
            return "socket"

        async def __aexit__(self, *a):
            return False

    def connect(url, proxy=None, open_timeout=None, **kw):
        seen.append((proxy, open_timeout))
        return _CM(proxy)
    monkeypatch.setattr(websockets, "connect", connect)
    m = cm.ConcordMirror.__new__(cm.ConcordMirror)
    m._open_override = None
    m.cfg = {"concord_mirror_proxy": "http://127.0.0.1:8119", "concord_mirror_tor": [T1, T2]}   # as thread.py builds it

    async def go():
        for _ in range(2):
            async with m._open("wss://asia.vectorapp.io/nostr") as ws:
                assert ws == "socket"
    asyncio.run(go())
    assert seen == [(T1, tun.TOR_OPEN_TIMEOUT), (T2, tun.TOR_OPEN_TIMEOUT), (T2, tun.TOR_OPEN_TIMEOUT)], seen
    # Only when no Tor circuit answers does the mirror dial this node's own address -- last.
    seen.clear(); works.clear(); works.add(None)
    asyncio.run(go())
    assert [p for p, _ in seen][:3] == [T2, T1, None], seen   # Tor 2 had worked: it leads, then Tor 1, then direct




def test_a_link_that_loses_its_cancellation_cannot_freeze_the_mirror():
    """Measured on poster.place 2026-10-14 21:37-21:46: no registration completed for nine minutes, and the three
    busiest relays of the node (jskitty, asia.vectorapp, computingcache -- in nearly every registration, so their
    links are replaced on nearly every one) had NO link, while the mirror's own _desired() wanted all three. A
    reconcile had popped them and was awaiting an old link's stop() that never returned -- the link's receive loop
    used asyncio.wait_for, which on Python 3.11 can swallow a cancellation that lands as recv completes -- holding
    the reconcile lock for ever. Every room on those relays answered `unavailable`, every device read all of them
    through the tunnel until its share was full: "room could not be read, 0 of 5 relays answered".

    Here the old link's session swallows its cancellation outright. A second community on the same relay must
    still register promptly, and the relay must have a link serving both."""
    stream_a, stream_b, member = key(), key(), key()
    real_session = cm._Link._session
    # The stubborn link must END once the test is over: asyncio.run's teardown cancels every task left and waits
    # for each, and one that swallows every cancellation held the gate for its whole 10-minute limit (deploy 89).
    over = {"v": False}

    async def stubborn(self, ws):
        if pub(stream_b) in self.authors:            # the REPLACEMENT link works normally
            return await real_session(self, ws)
        while not over["v"]:                         # the old one: cancellation lost, runs on regardless
            try:
                await real_session(self, ws)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)

    async def go():
        up = await Upstream([wrap(stream_a), wrap(stream_b)]).start()
        node = await Node({pub(member)}).start()
        cm._Link._session = stubborn                     # the first link will ignore being stopped
        try:
            ok, why = await node.mirror.register(registration(member, [up.url], [pub(stream_a)], group="aa" * 32),
                                                 is_local=True)
            assert ok, why
            assert await until(lambda: node.mirror.author_ready(pub(stream_a)))
            try:
                ok, why = await asyncio.wait_for(node.mirror.register(
                    registration(member, [up.url], [pub(stream_b)], group="bb" * 32), is_local=True), 10)
            except asyncio.TimeoutError:
                raise AssertionError("a registration hung behind a link that would not stop -- the mirror froze")
            assert ok, why
            assert up.url in node.mirror.links, "the relay was left with no link"
            assert await until(lambda: node.mirror.author_ready(pub(stream_a)) and node.mirror.author_ready(pub(stream_b)),
                               15), "the replacement link never served both communities"
        finally:
            over["v"] = True
            cm._Link._session = real_session
            await asyncio.wait_for(node.close(), 20)
            await up.close()
    run(go())


def test_a_relay_on_this_nodes_blocklist_is_not_named_for_clients_to_read_directly():
    """relay.ditto.pub is on poster.place's relay blocklist and is listed by every community one account belongs
    to. The registration reply named it under "read directly:", so every device read it ITSELF, through the
    tunnel, one socket per stream on every refresh, often for the whole history: ~2,700 tunnels an hour from one
    account, measured 2026-10-22. Blocked is "this node will not read it", not "cannot": the community's other
    relays carry the same streams and are mirrored. A relay a client can reach and this node cannot (an odd port)
    is still named."""
    stream, member = key(), key()

    def url_ok(u, c):
        if "blocked" in u:
            return cm.BLOCKLISTED
        if u.endswith(":7777"):
            return "only relays on port 443 are mirrored"
        return ""

    async def go():
        good = await Upstream([wrap(stream)]).start()
        node = await Node({pub(member)}, url_ok=url_ok).start()
        try:
            blocked, odd = "wss://relay.blocked.example", "wss://relay.odd.example:7777"
            ok = await node.publish(registration(member, [good.url, blocked], [pub(stream)]))
            assert ok[2] is True, ok
            assert "read directly:" not in ok[3], ("a blocklisted relay was handed to clients to read", ok)
            ok = await node.publish(registration(member, [good.url, blocked, odd], [pub(stream)], group="ab" * 32))
            assert ok[2] is True, ok
            assert ok[3].split("read directly:", 1)[1].split() == [odd], ok
        finally:
            await node.close()
            await good.close()
    run(go())


def test_a_stream_dropped_and_named_again_is_mirrored_again_not_served_empty():
    """SORSMENEDÉK, poster.place 2026-10-22: 5 of the ~200 wraps its four relays hold, served as the whole room.

    A device re-registered the community with only its 4 control streams (15:40:04) and 11 s later with all 22
    (15:40:15). The shrink deleted the dropped streams' copies (_reconcile); the re-walk of their history then
    skipped every wrap as already seen -- `_seen` remembered ids this process had stored or found stored, and
    nothing forgot them when the copies went. The links were in step, so the node answered EOSE with nothing:
    every channel empty until the relay process restarted. Measured live the same night: of 27 groups on
    jskitty.com, every one held all its wraps here except that one (4 of 203)."""
    channel, control, member = key(), key(), key()
    now = int(time.time())
    history = [wrap(channel, created_at=now - 5000 + i * 10) for i in range(12)]

    async def go():
        up = await Upstream(history + [wrap(control, created_at=now - 100)]).start()
        node = await Node({pub(member)}).start()
        group = "5e" * 32
        try:
            both = [pub(control), pub(channel)]
            assert (await node.publish(registration(member, [up.url], both, group=group)))[2] is True
            assert await until(lambda: node.mirror.author_ready(pub(channel)) and len(node.store.events) == 13)
            # the device that knows only the control stream
            assert (await node.publish(registration(member, [up.url], [pub(control)], group=group)))[2] is True
            assert await until(lambda: not any(e["pubkey"] == pub(channel) for e in node.store.events.values()))
            # ...and the full set again
            assert (await node.publish(registration(member, [up.url], both, group=group)))[2] is True
            assert await until(lambda: node.mirror.author_ready(pub(channel)))
            await until(lambda: sum(e["pubkey"] == pub(channel) for e in node.store.events.values()) == 12, 5)
            got, last = await node.req([{"kinds": [1059], "authors": [pub(channel)], "limit": 100}])
            # never a short answer presented as the room's whole history
            assert last[0] == "CLOSED" or sorted(e["id"] for e in got) == sorted(e["id"] for e in history), \
                (last, len(got))
            assert sorted(e["id"] for e in got) == sorted(e["id"] for e in history), (last, len(got))
        finally:
            await node.close()
            await up.close()
    run(go())


def test_a_stream_this_node_stopped_mirroring_is_unavailable_not_an_empty_room():
    """The other half of the Sorsmenedék gap. A registration that no longer names a stream deletes this node's copy
    of it -- but a device that registered it earlier still trusts this node for it (its own mirror record) and
    reads it here. The node used to answer that read EOSE from a store it had just emptied: the room "loaded",
    blank. A stream whose copy this node DELETED is "could not ask" -- `unavailable:` -- for every reader (a
    `pcmp…` reader too: what it would be served is nothing). A stream it never mirrored stays the store's business
    (test_an_upstream_that_cannot_be_reached_is_never_no_messages), and kind-1059 DMs, read by #p, are untouched."""
    channel, control, member = key(), key(), key()
    history = [wrap(channel) for _ in range(3)]

    async def go():
        up = await Upstream(history + [wrap(control)]).start()
        node = await Node({pub(member)}).start()
        group = "6d" * 32
        try:
            both = [pub(control), pub(channel)]
            assert (await node.publish(registration(member, [up.url], both, group=group)))[2] is True
            assert await until(lambda: node.mirror.author_ready(pub(channel)) and len(node.store.events) == 4)
            assert (await node.publish(registration(member, [up.url], [pub(control)], group=group)))[2] is True
            assert await until(lambda: not any(e["pubkey"] == pub(channel) for e in node.store.events.values()))
            for prefix in ("s", "pcmp"):
                got, last = await node.req([{"kinds": [1059], "authors": [pub(channel)]}], prefix=prefix)
                assert last[0] == "CLOSED" and last[2].startswith("unavailable:"), (prefix, last, len(got))
            # the stream still mirrored is served as before
            got, last = await node.req([{"kinds": [1059], "authors": [pub(control)]}])
            assert last[0] == "EOSE" and len(got) == 1, last
            # a DM gift wrap addressed to someone (#p, no authors) is the store's business, as always
            dm = wrap(key())
            node.store.events[dm["id"]] = dm
            got, last = await node.req([{"kinds": [1059], "#p": [dm["tags"][0][1]]}])
            assert last[0] == "EOSE" and [e["id"] for e in got] == [dm["id"]], last
            # named again: in step again, every wrap back
            assert (await node.publish(registration(member, [up.url], both, group=group)))[2] is True
            assert await until(lambda: node.mirror.author_ready(pub(channel)))
            await until(lambda: sum(e["pubkey"] == pub(channel) for e in node.store.events.values()) == 3, 5)
            got, last = await node.req([{"kinds": [1059], "authors": [pub(channel)]}])
            assert last[0] == "EOSE" and len(got) == 3, (last, len(got))
            # and the dropped set survives a restart of the relay process (it is saved with the registrations)
            assert (await node.publish(registration(member, [up.url], [pub(control)], group=group)))[2] is True
            assert await until(lambda: not any(e["pubkey"] == pub(channel) for e in node.store.events.values()))
            await node.mirror._save_now()
            fresh = cm.ConcordMirror(node.store, None, node.cfg, url_ok=lambda u, c: "",
                                     open_ws=lambda url: websockets.connect(url, open_timeout=3))
            await fresh.start()
            try:
                assert fresh.req_refusal([{"kinds": [1059], "authors": [pub(channel)]}]).startswith("unavailable:")
            finally:
                await fresh.stop()
        finally:
            await node.close()
            await up.close()
    run(go())


def test_the_relay_hands_the_mirror_the_proxys_tor_list(monkeypatch):
    """The mirror runs in the relay process: its Tor list comes through the config the relay thread builds, from the
    same socks_backends() the proxy's backends are built from."""
    from app.services.nostr_relay import thread
    from app.services import http_proxy_service as hp
    monkeypatch.setattr(hp, "socks_backends", lambda ss=None: [(9052, "us"), (9062, "ca")])
    assert thread._concord_tor_hops() == ["socks5h://127.0.0.1:9052", "socks5h://127.0.0.1:9062"]
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/services/nostr_relay/thread.py").read_text()
    assert '"concord_mirror_tor": _concord_tor_hops()' in src
