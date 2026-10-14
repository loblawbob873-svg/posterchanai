"""PosterChanOS's local relay holds the user's Notes, vault, playlists and Budget even when their account's
kind-30078 set is dominated by something else -- the operator's own server tables.

Measured 2026-10-14 on the owner's desktop (.102) after an outage: its local relay held TWO notes. The owner
is the node's OPERATOR, and every DocTable the server keeps (accounts, the fediverse ledger, settings) is a
kind-30078 the operator key signs, so the one "private" sync filter `{authors:[owner], kinds:[30078]}` named
hundreds of thousands of documents. The instance answers a negentropy sync with the NEWEST `neg_max_items`
of them, which is server churn; a note written last month is never in that window, so it was never offered,
and Notes showed what that window happened to have loaded. The fix syncs the client's own documents through
filters of their own (their `l` labels and the pinned single documents), which the tables cannot crowd out.

A REAL instance (RelayServer + PcdbRelayStore, NEG and NIP-42 as on a node) with neg_max_items lowered so the
crowding happens with a few hundred documents instead of a few hundred thousand; the SHIPPED os-mode process.
"""
import asyncio
import shutil
import time

from tests import os_relay_harness as H

CAP = 50


def test_old_notes_reach_the_cache_under_more_new_server_documents_than_the_instance_will_list():
    tmp = H.tmpdir()
    inst = H.Instance(tmp, neg_max_items=CAP).start()
    relay = H.LocalRelay(tmp, instance_url=inst.url)
    try:
        owner = H.pk(H.OWNER_SK)
        now = int(time.time())
        mine = {
            "note": H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:note:old"], ["l", "pcai-notes"]], created_at=now - 86400 * 40),
            "folder": H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:notefolder:f"], ["l", "pcai-notes"]], created_at=now - 86400 * 40),
            "vault": H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:pw:item1"], ["l", "pcai-pw"]], created_at=now - 86400 * 30),
            "playlist": H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:playlist:p1"], ["l", "pcai-music"]], created_at=now - 86400 * 30),
            "budget": H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:budget"]], created_at=now - 86400 * 20),
            "files": H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:files-index"]], created_at=now - 86400 * 20),
        }
        tables = [H.ev(H.OWNER_SK, 30078, "ct", [["d", "pcai:t:fedi_bridge_delivered:%d" % i]], created_at=now - 60 - i)
                  for i in range(CAP * 4)]
        assert inst.add(list(mine.values()) + tables) == len(mine) + len(tables)
        relay.start()

        async def session():
            async with H.Client(relay.url, H.OWNER_SK, sign_for=inst.relay_url) as c:
                ok, why = await c.auth()
                assert ok, why
                want = {e["id"] for e in mine.values()}
                held = set()
                deadline = time.time() + 90
                while time.time() < deadline:
                    held = {e["id"] for e in await c.query({"ids": sorted(want)})}
                    if want <= held:
                        break
                    await asyncio.sleep(1)
                return held
        held = asyncio.run(session())
        missing = sorted(k for k, e in mine.items() if e["id"] not in held)
        assert not missing, ("the user's own documents never reached the desktop's cache because newer server "
                             "documents filled the instance's sync window", missing, relay.output()[-2000:])
    finally:
        relay.stop()
        inst.close()
        shutil.rmtree(tmp, ignore_errors=True)
