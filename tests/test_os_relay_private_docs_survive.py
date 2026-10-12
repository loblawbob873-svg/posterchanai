"""The PosterChanOS local relay keeps the user's private documents through eviction AND a restart.

It replaces the client's IndexedDB as the desktop's event cache, so it inherits that cache's hardest rule:
a document only its author can decrypt (a note, a vault item, the drive index) is not refetchable the way a
timeline is, and must never be the thing a size cap throws away (store.js `_isPinned`; the relay's copy of
the rule is os_pins.py, held to the client's by test_os_relay_pins_match_the_client.py).

Runs the SHIPPED process (`relay_main.py --os-mode`) with a 1 MB cap, writes a pinned note, an UNPINNED
app document and ~4 MB of firehose, lets the eviction pass run, restarts the process, and reads back:
  * the note is there (and only for its authenticated author -- another account on this machine can reach
    the port, so a NIP-78 read still needs AUTH);
  * eviction really happened (the live set fits the cap), oldest-UNUSED first: an old post that was READ
    after it was written outlives newer posts nobody read -- least recently used, not "the newest N".
"""
import asyncio
import json
import os
import shutil
import time

from tests import os_relay_harness as H


def test_pinned_documents_survive_eviction_and_a_restart_and_eviction_is_lru():
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp, extra_env={"PC_RELAY_MAX_MB": "1", "PC_RELAY_PRUNE_SECONDS": "2"})
    owner = H.pk(H.OWNER_SK)
    now = int(time.time())
    note = H.ev(H.OWNER_SK, 30078, "sealed-note-" + "n" * 200, [["d", "pcai:note:keep-me"], ["l", "pcai-notes"]],
                created_at=now - 86400 * 30)
    unpinned_doc = H.ev(H.OWNER_SK, 30078, "x" * 300, [["d", "pcai:kv:scratch"]], created_at=now - 86400 * 30)
    old_read = H.ev(H.FRIEND_SK, 1, "old but read " + os.urandom(300).hex(), created_at=now - 86400 * 20)
    filler = [H.ev(H.STRANGER_SK, 1, "firehose %d %s" % (i, os.urandom(400).hex()), created_at=now - 5000 + i)
              for i in range(4000)]
    try:
        relay.start()

        async def phase1():
            async with H.Client(relay.url, H.OWNER_SK) as c:
                for e in (note, unpinned_doc, old_read):
                    ok, why = await c.publish(e)
                    assert ok, why
                # The firehose arrives while somebody keeps going back to the old post: that is USE, and it must
                # count for more than the firehose's newer created_at (eviction passes run every 2 s meanwhile).
                for i in range(0, len(filler), 200):
                    for e in filler[i:i + 200]:
                        await c.ws.send(json.dumps(["EVENT", e]))
                    await c.publish(filler[min(i + 199, len(filler) - 1)])
                    assert [x["id"] for x in await c.query({"ids": [old_read["id"]]})] == [old_read["id"]], \
                        "an old post somebody is reading was evicted before newer posts nobody read"
                await asyncio.sleep(8)          # several eviction passes (PC_RELAY_PRUNE_SECONDS=2)
        asyncio.run(phase1())
        assert "evicted" in relay.output(), relay.output()[-2000:]
        relay.stop()
        relay.start()

        async def phase2():
            async with H.Client(relay.url, H.STRANGER_SK) as other:
                await other.auth()
                try:
                    stranger_read = await other.query({"authors": [owner], "kinds": [30078]})
                except AssertionError as refused:          # CLOSED auth-required: the right answer
                    assert "auth-required" in str(refused), refused
                    stranger_read = []
            async with H.Client(relay.url, H.OWNER_SK) as c:
                ok, why = await c.auth()
                assert ok, why
                docs = await c.query({"authors": [owner], "kinds": [30078]})
                old = await c.query({"ids": [old_read["id"]]})
                live = await c.query({"authors": [H.pk(H.STRANGER_SK)], "limit": 5000}, timeout=60)
            return stranger_read, docs, old, live
        stranger_read, docs, old, live = asyncio.run(phase2())

        dtags = {next(t[1] for t in e["tags"] if t[0] == "d") for e in docs}
        assert "pcai:note:keep-me" in dtags, ("the cap evicted a pinned note (or the restart lost it)", dtags)
        assert stranger_read == [], "another account read the owner's private documents without being them"
        assert len(live) < len(filler), "nothing was evicted -- the cap is not running"
        assert [e["id"] for e in old] == [old_read["id"]], \
            "an old post that was READ was evicted before newer posts nobody read -- that is not LRU"
        # Bounded (~4 MB written under a 1 MB cap), and what went first is what was used least recently: the
        # first-written firehose, never the last-written.
        assert len(live) < len(filler) * 0.6, ("eviction did not bring the cache near its cap", len(live))
        kept = {e["id"] for e in live}
        idx = [i for i, e in enumerate(filler) if e["id"] in kept]
        assert all(e["id"] in kept for e in filler[-300:]), ("the most recently written events were evicted", len(idx), idx[:5], idx[-5:])
        assert not any(e["id"] in kept for e in filler[:300]), "the least recently used events survived"
    finally:
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)
