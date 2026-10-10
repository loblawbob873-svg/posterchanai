"""The relay's bridge scans must not decode every stored event.

2026-10-10: server1's relay was promoted to PosterChanDB PRIMARY with 3.28M events. `_bridged_pubkeys_sync` and
`_social_mirror_pubkeys_sync` decoded EVERY event in Python -- on start and again on every reload-blocks the app
sends -- so the relay's main thread sat at 100%, a `kinds:[0] limit 1` query took 4-6 s, and the fediverse inbox
answered 503 because its one-document ledger reads timed out. The Postgres store answered the same question with
`kind IN (0,3,10002) OR tags LIKE '%"proxy"%'`; these tests hold the PosterChanDB store to that cost, and to the
same answer (the parity test in test_pcdb_relay_store.py covers the answer against Postgres).
"""
import asyncio
import time

from app.services.nostr_relay import pcdb_store as P
from tests.test_posterchandb import ev_id

BLOCKED = {"mostr.pub"}


def _mk(pk, kind=1, tags=None, content="hello", n=0):
    e = {"pubkey": pk, "created_at": int(time.time()) - 1000 + n, "kind": kind, "tags": tags or [],
         "content": content, "sig": "0" * 128}
    e["id"] = ev_id(e)
    return e


def _store(tmp_path):
    loop = asyncio.new_event_loop()
    d = str(tmp_path / "pcdb")
    P.init_new(d)
    ps = P.PcdbRelayStore(d, maintenance=False)
    ps.open(loop)
    return ps, loop


def _count_decodes(ps):
    st = ps.store
    real = st.get
    box = {"n": 0}

    def counted(seq):
        box["n"] += 1
        return real(seq)
    st.get = counted
    return box


def test_bridge_scans_decode_only_candidates_and_then_only_new_events(tmp_path):
    ps, loop = _store(tmp_path)
    try:
        plain = ["%064x" % (i + 1) for i in range(400)]
        for i, pk in enumerate(plain):                       # ordinary posts: nothing to do with a bridge
            ps._add_event_sync(_mk(pk, 1, content="just a post %d" % i, n=i), "wot")
        bridged = "b" * 64
        proxied = _mk(bridged, 1, tags=[["proxy", "https://mostr.pub/objects/1", "activitypub"]], n=500)
        ps._add_event_sync(proxied, "wot")
        prof = _mk("c" * 64, 0, content='{"name":"x","nip05":"x@mostr.pub"}', n=501)
        ps._add_event_sync(prof, "wot")

        box = _count_decodes(ps)
        got = loop.run_until_complete(ps.bridged_pubkeys(BLOCKED))
        assert got == {bridged, "c" * 64}
        assert box["n"] <= 5, "decoded %d of 402 events for the bridge scan" % box["n"]

        box["n"] = 0
        mirrors = loop.run_until_complete(ps.social_mirror_pubkeys())
        assert bridged in mirrors
        assert box["n"] <= 5, "decoded %d of 402 events for the social-mirror scan" % box["n"]

        # A second pass (every reload-blocks) looks only at what was stored since.
        late = "d" * 64
        ps._add_event_sync(_mk(late, 1, tags=[["proxy", "https://mostr.pub/objects/2", "activitypub"]], n=600),
                           "wot")
        box["n"] = 0
        assert loop.run_until_complete(ps.bridged_pubkeys(BLOCKED)) == {bridged, "c" * 64, late}
        assert box["n"] <= 2, "a reload re-decoded %d events it had already scanned" % box["n"]

        # A deleted event stops counting, without a rescan.
        ps._kill_seqs([ps.store.seq_of(proxied["id"])])
        assert loop.run_until_complete(ps.bridged_pubkeys(BLOCKED)) == {"c" * 64, late}
        assert bridged not in loop.run_until_complete(ps.social_mirror_pubkeys())

        # Different domains = a different question: answered from scratch, not from the old memo.
        assert loop.run_until_complete(ps.bridged_pubkeys({"brid.gy"})) == set()
    finally:
        ps.close()
        loop.close()
