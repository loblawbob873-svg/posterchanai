"""DocTable: an app table kept as encrypted operator documents on this node's relay (#161, owner's call 2026-10-10).

Run against the SHIPPED RelayServer over a real socket with the REAL PosterChanDB store behind it -- the relay's
NIP-42 check, its NIP-78 read gate, its live fan-out and NIP-09 deletions are what this code depends on, and a fake
that agreed with a wrong assumption would pass. Rules pinned:
  * nothing is answered before a strict load succeeded: "could not ask" raises, never "no rows";
  * a write lands on the relay before memory, and is readable by a fresh process (a second table instance);
  * a write or delete made by ANOTHER process reaches this one through the live stream;
  * the live subscription is narrowed to the table's own namespace (`#d~` in the relay's live matcher).
"""
import asyncio
import os
import socket
import threading
import time

import pytest

from app.services import doc_table
from app.services.nostr import bech32, bip340
from app.services.relay_reader import Unavailable

SK = bytes.fromhex("31" * 32)
OP = bip340.pubkey_from_seckey(SK).hex()


class _Gate:
    def is_member(self, _pk): return True
    def is_operator(self, pk): return pk == OP
    def is_blocked(self, _pk): return False
    def is_puppet_event(self, _ev): return False


class _Relay:
    """RelayServer + the real PosterChanDB-primary store, on a thread."""
    def __init__(self, path):
        import websockets
        from app.services.nostr_relay.server import RelayServer
        from app.services.nostr_relay import pcdb_store as P
        P.init_new(path)
        ready, self._h = threading.Event(), {}

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def main():
                st = P.PcdbRelayStore(path, max_events=0, retention_days=0)
                st.open(loop)
                st.preserve_pubkeys = {OP}
                srv = RelayServer(st, _Gate(), {"wot_enabled": False, "node_pubkey": OP, "operator": OP})
                ws = await websockets.serve(srv.handle, "127.0.0.1", 0, process_request=srv.process_request)
                self.port = ws.sockets[0].getsockname()[1]
                self._h["stop"] = asyncio.Event()
                ready.set()
                await self._h["stop"].wait()
                ws.close()
                await ws.wait_closed()
                st.close()
            self._h["loop"] = loop
            loop.run_until_complete(main())
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()
        assert ready.wait(20), "the relay did not start"

    def close(self):
        self._h["loop"].call_soon_threadsafe(self._h["stop"].set)
        self._t.join(10)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    r = _Relay(str(tmp_path / "relay"))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(r.port))
    doc_table.DocTable._registry.clear()
    try:
        yield r
    finally:
        doc_table.DocTable._registry.clear()
        r.close()


def _fresh(name):
    """A second process's view of the same table (the registry hands back one instance per name)."""
    doc_table.DocTable._registry.pop(name, None)
    return doc_table.DocTable(name)


def _until(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_an_unreachable_relay_is_unavailable_never_an_empty_table(tmp_path, monkeypatch):
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    doc_table.DocTable._registry.clear()
    t = doc_table.DocTable("rowsx")
    with pytest.raises(Unavailable):
        t.all()
    with pytest.raises(Unavailable):
        t.get("1")


def test_rows_are_written_to_the_relay_and_a_fresh_process_reads_them(relay):
    t = doc_table.DocTable("rowsx")
    assert t.all() == {}
    t.put("7", {"user": "npub1x", "due": 1700000000, "text": "call the bank"})
    t.put("a/b c", {"n": 2})
    assert t.get("7")["text"] == "call the bank"
    other = _fresh("rowsx")
    assert other.all() == {"7": {"user": "npub1x", "due": 1700000000, "text": "call the bank"}, "a/b c": {"n": 2}}
    assert [k for k, _ in other.where(lambda r: r.get("n") == 2)] == ["a/b c"]
    assert _fresh("pushsubs").all() == {}, "another table's namespace leaked in"


def test_a_write_and_a_delete_by_another_process_arrive_live(relay):
    mine = doc_table.DocTable("bots")
    assert mine.all() == {}
    # "another process": a separate instance writing through the relay
    theirs = _fresh("bots")
    doc_table.DocTable._registry["bots"] = mine         # keep `mine` registered as this process's view
    asyncio.run(theirs.aput("alpha", {"enabled": True}))
    assert _until(lambda: mine.get("alpha") == {"enabled": True}), "a write from another process never arrived"
    asyncio.run(theirs.adelete("alpha"))
    assert _until(lambda: mine.get("alpha") is None), "a deletion from another process never arrived"


def test_rows_are_encrypted_on_the_relay(relay):
    t = doc_table.DocTable("apikeys")
    t.put("k1", {"secret": "do-not-leak-7731"})
    from app.services import relay_reader
    evs = relay_reader.query([{"authors": [OP], "kinds": [30078], "#d~": ["pcai:t:apikeys:"]}],
                             port=relay.port, auth_seckey=SK)
    assert evs and all("do-not-leak-7731" not in e["content"] for e in evs)


def test_a_sync_call_on_an_event_loop_is_refused_not_a_deadlock(relay):
    t = doc_table.DocTable("rowsx")

    async def inside():
        with pytest.raises(RuntimeError, match="event-loop"):
            t.put("x", {"a": 1})
        await t.aput("x", {"a": 1})
        return await t.aget("x")
    assert asyncio.run(inside()) == {"a": 1}


def test_the_live_matcher_honours_a_d_prefix():
    from app.services.nostr_relay.server import _compile_filter, _matches
    ev = {"id": "x", "pubkey": OP, "kind": 30078, "created_at": 5, "tags": [["d", "pcai:t:bots:1"]], "content": ""}
    assert _matches([_compile_filter({"#d~": ["pcai:t:bots:"]})], ev)
    assert not _matches([_compile_filter({"#d~": ["pcai:t:reminders:"]})], ev)


def test_a_delete_during_an_outage_is_unavailable_not_done(relay, monkeypatch):
    """nostr_store.delete_doc answered success when it could not even look the document up -- the row stayed on
    the relay and came back at the next load. A delete that could not ask must say so."""
    t = doc_table.DocTable("pushq")
    assert t.all() == {}
    t.put("1", {"a": 1})
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(dead))
    with pytest.raises(Unavailable):
        t.delete("1")
    assert t.get("1") == {"a": 1}, "memory dropped a row the relay still holds"
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(relay.port))
    t.delete("1")
    assert _fresh("pushq").get("1") is None
