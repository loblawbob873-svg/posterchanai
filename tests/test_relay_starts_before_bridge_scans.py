"""The relay listens BEFORE it scans for bridge accounts, and gates correctly while it scans.

2026-10-10: on the 3.28M-event PosterChanDB relay the two bridge scans ran before `serve(...)` and kept the relay
deaf for ~4 minutes on every start. The marks are now seeded from the last scan's saved result, the scan runs as a
task behind the listener, a seeded mark the new scan no longer finds is dropped (a domain taken off the blocklist),
and the result is saved for the next start.
"""
import asyncio
import inspect
import re

from app.services.nostr_relay import thread as relay_thread
from app.services.nostr_relay.wot import WotGate

A, B, C, OP = "a" * 64, "b" * 64, "c" * 64, "f" * 64


class _Store:
    def __init__(self, saved=None, weak=(), ident=(), mirrors=()):
        self.kv = {} if saved is None else {"bridge_marks": saved}
        self.weak, self.ident, self.mirrors = set(weak), set(ident), set(mirrors)
        self.preserve_pubkeys = set()

    async def kv_get(self, k):
        return self.kv.get(k)

    async def kv_set(self, k, v):
        self.kv[k] = v

    async def bridged_pubkeys(self, domains):
        return set(self.weak)

    async def bridge_identity_pubkeys(self, domains):
        return set(self.ident)

    async def nip05_domains(self):
        return []

    async def social_mirror_pubkeys(self):
        return set(self.mirrors)


def _gate():
    g = WotGate()
    g.set_operator([OP])
    return g


def test_seeded_marks_gate_at_once_and_a_rescan_replaces_them():
    async def body():
        st = _Store(saved=" ".join([A, B, OP]), ident=[A], mirrors=[C])
        g = _gate()
        seeded = await relay_thread._seed_bridge_marks(st, g)
        assert g.is_bridged(A) and g.is_bridged(B), "the saved marks did not gate before the scan"
        assert not g.is_bridged(OP), "an operator was bridge-marked from the saved set"
        await relay_thread._rescan_bridges(st, g, {"blocked_relays": ["mostr.pub"], "block_bridged": True}, seeded)
        assert g.is_bridged(A) and g.is_bridged(C)
        assert not g.is_bridged(B), "a mark the new scan no longer finds survived the rescan"
        assert set(st.kv["bridge_marks"].split()) == {A, C}
    asyncio.run(body())


def test_an_unreadable_seed_still_scans():
    class Broken(_Store):
        async def kv_get(self, k):
            raise OSError("down")

    async def body():
        st = Broken(ident=[A])
        g = _gate()
        seeded = await relay_thread._seed_bridge_marks(st, g)
        assert seeded == frozenset()
        await relay_thread._rescan_bridges(st, g, {"blocked_relays": ["mostr.pub"]}, seeded)
        assert g.is_bridged(A)
    asyncio.run(body())


def test_nothing_before_the_listener_awaits_a_bridge_scan():
    src = inspect.getsource(relay_thread._main)
    before = src[:src.index("ws = await serve(")]
    assert not re.search(r"await\s+(_safe\()?_mark_(blocked_relays|social_mirrors)", before), \
        "a bridge scan is awaited before the relay listens"
    after = src[src.index("ws = await serve("):]
    assert "_rescan_bridges(" in after, "the bridge scan never runs"


def test_before_listening_the_relay_never_asks_itself_for_its_tables(monkeypatch):
    """The accounts, bots and legacy-storage tables are served by THIS relay. Asked before it listened, each read
    spent ~6 s in page retries against a refused socket: 18 s of a 20 s start (2026-10-10)."""
    from app.services import bot_table, user_table, user_settings_table
    asked = []
    monkeypatch.setattr(user_table, "all_users", lambda db: asked.append("users") or [])
    monkeypatch.setattr(bot_table, "all_bots", lambda db: asked.append("bots") or [])
    monkeypatch.setattr(user_settings_table, "by_key", lambda db, k: asked.append("storage") or {})
    monkeypatch.setattr(relay_thread, "_reload_when_readable", lambda: None)
    monkeypatch.setitem(relay_thread._listening, "on", False)
    monkeypatch.setitem(relay_thread._last_known, "users", {A})
    assert relay_thread._user_pubkeys(None) == {A}
    relay_thread._bot_pubkeys(None)
    relay_thread._legacy_storage_pubkeys(None)
    assert asked == [], "the relay asked itself before it was listening: %s" % asked
    monkeypatch.setitem(relay_thread._listening, "on", True)
    relay_thread._user_pubkeys(None)
    relay_thread._bot_pubkeys(None)
    relay_thread._legacy_storage_pubkeys(None)
    assert sorted(asked) == ["bots", "storage", "users"]


def test_listening_is_declared_right_after_serve():
    src = inspect.getsource(relay_thread._main)
    after = src[src.index("ws = await serve("):]
    assert after.index('_listening["on"] = True') < after.index("_rescan_bridges(")
