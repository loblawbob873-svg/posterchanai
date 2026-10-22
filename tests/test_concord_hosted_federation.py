"""COMMUNITIES HOSTED ON THIS NODE FEDERATE TO BACKUP RELAYS (concord_mirror `op: replicate`, 2026-10-21).

"why can't our relay federate our posterchan rooms?" / "do 1, and make sure posterchan room federates" / "make sure
to build test cases". A community created on an instance lists ONE relay -- this node's -- so it lived nowhere
else. A member's app now asks the node to replicate it; the node copies every wrap (opaque: it holds no key) to its
backup relays, pushes the history they lack, and reads back what other apps post there. This relay stays the
community's home: a backup that is down never fails a post or a read, and nothing hosted is ever deleted or budgeted
as if it were a mirror copy.

The SHIPPED RelayServer + ConcordMirror over real sockets, real websocket relays as the backups (tests/
test_concord_mirror.py's harness).
"""
from __future__ import annotations

import json
import time

from app.services.nostr.event import build_event
from app.services.nostr_relay import concord_mirror as cm
from tests.test_concord_mirror import Node, Upstream, closed_port_url, key, pub, run, until, wrap


def replicate(member_sk, authors, *, group=None, op="replicate"):
    body = {"v": 1, "op": op, "group": group or ("cd" * 32), "authors": authors}
    return build_event(member_sk, cm.KIND_REGISTER, json.dumps(body), tags=[["t", "concord-mirror"]])


def test_history_new_posts_and_posts_made_elsewhere_all_federate(monkeypatch):
    monkeypatch.setattr(cm, "BACKFILL_PACE", 0)
    stream, member = key(), key()
    now = int(time.time())
    history = [wrap(stream, created_at=now - 900 + i) for i in range(7)]

    async def go():
        a, b = await Upstream().start(), await Upstream().start()
        node = await Node({pub(member)}, cfg={"concord_backup_relays": [a.url, b.url]}).start()
        try:
            for ev in history:                        # the community, as it already lived here alone
                await node.store.add_event(ev, origin="direct")
            ok = await node.publish(replicate(member, [pub(stream)]))
            assert ok[2] is True and ok[3].startswith("replicating"), ok
            # 1. the history this node held reaches EVERY backup, oldest first
            want = [e["id"] for e in sorted(history, key=lambda e: e["created_at"])]
            assert await until(lambda: [e["id"] for e in a.writes] == want and [e["id"] for e in b.writes] == want), \
                ([e["id"] for e in a.writes], want)
            # 2. a new post: accepted at once (this is its home), then copied out
            mine = wrap(stream)
            ok = await node.publish(mine)
            assert ok[2] is True, ok
            assert await until(lambda: any(e["id"] == mine["id"] for e in a.writes)
                               and any(e["id"] == mine["id"] for e in b.writes))
            # 3. posted on a backup by another app: it reaches this node's store
            theirs = wrap(stream)
            await a.inject(theirs)
            assert await until(lambda: theirs["id"] in node.store.events)
        finally:
            await node.close()
            await a.close()
            await b.close()
    run(go())


def test_a_backup_that_is_down_never_fails_a_post_or_a_read():
    stream, member = key(), key()

    async def go():
        node = await Node({pub(member)}, cfg={"concord_backup_relays": [closed_port_url()]}).start()
        try:
            assert (await node.publish(replicate(member, [pub(stream)])))[2] is True
            mine = wrap(stream)
            ok = await node.publish(mine)
            assert ok[2] is True, ("a hosted post was failed because its backup was down", ok)
            got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)], "limit": 50}])
            assert last[0] == "EOSE", ("this relay is the home: its copy is the answer", last)
            assert [e["id"] for e in got] == [mine["id"]]
        finally:
            await node.close()
    run(go())


def test_leaving_or_lapsing_never_deletes_the_originals(monkeypatch):
    stream, member = key(), key()

    async def go():
        a = await Upstream().start()
        node = await Node({pub(member)}, cfg={"concord_backup_relays": [a.url]}).start()
        try:
            assert (await node.publish(replicate(member, [pub(stream)])))[2] is True
            mine = wrap(stream)
            assert (await node.publish(mine))[2] is True
            # leave
            assert (await node.publish(replicate(member, [pub(stream)], op="leave")))[2] is True
            assert mine["id"] in node.store.events, "leaving deleted a hosted community's own messages"
            # and a registration that lapsed (14 days unrefreshed)
            assert (await node.publish(replicate(member, [pub(stream)])))[2] is True
            for g in node.mirror.regs.values():
                for reg in g.values():
                    reg["at"] = 0
            await node.mirror._expire()
            await node.mirror._reconcile()
            assert mine["id"] in node.store.events, "a lapsed replication deleted the originals"
            assert not node.store.deleted or all(pub(stream) not in d for d in node.store.deleted), node.store.deleted
        finally:
            await node.close()
            await a.close()
    run(go())


def test_hosted_messages_are_not_budgeted_like_mirror_copies(monkeypatch):
    monkeypatch.setattr(cm, "MAX_EVENTS_PER_COMMUNITY", 2)
    stream, member = key(), key()

    async def go():
        a = await Upstream().start()
        node = await Node({pub(member)}, cfg={"concord_backup_relays": [a.url]}).start()
        try:
            assert (await node.publish(replicate(member, [pub(stream)])))[2] is True
            for _ in range(4):
                await a.inject(wrap(stream))          # other apps posting on the backup
            assert await until(lambda: sum(1 for e in node.store.events.values() if e["pubkey"] == pub(stream)) == 4)
            assert node.mirror.counts.get(pub(stream), 0) == 0
            _got, last = await node.req([{"kinds": [1059], "authors": [pub(stream)]}])
            assert last[0] == "EOSE", last
        finally:
            await node.close()
            await a.close()
    run(go())


def test_a_backup_that_lost_its_copy_is_filled_again(monkeypatch):
    monkeypatch.setattr(cm, "BACKFILL_PACE", 0)
    stream, member = key(), key()

    async def go():
        a = await Upstream().start()
        node = await Node({pub(member)}, cfg={"concord_backup_relays": [a.url]}).start()
        try:
            assert (await node.publish(replicate(member, [pub(stream)])))[2] is True
            posts = [wrap(stream) for _ in range(3)]
            for ev in posts:
                assert (await node.publish(ev))[2] is True
            assert await until(lambda: len(a.events) == 3)
            a.events.clear()                          # the backup lost everything
            link = node.mirror.links[a.url]
            await link.stop()
            link.stopped = False
            link.start()                              # reconnect: history walk, then backfill
            assert await until(lambda: {e["id"] for e in a.events} == {e["id"] for e in posts}, 15), len(a.events)
        finally:
            await node.close()
            await a.close()
    run(go())


def test_only_members_of_this_node_can_ask_for_replication():
    stream, stranger = key(), key()

    async def go():
        node = await Node(set(), cfg={"concord_backup_relays": [closed_port_url()]}).start()
        try:
            ok = await node.publish(replicate(stranger, [pub(stream)]))
            assert ok[2] is False and "only accounts on this instance" in ok[3], ok
            assert not node.mirror.hosted_ever
        finally:
            await node.close()
    run(go())


def test_the_hosted_set_survives_a_restart():
    """Without it, a restart would forget which streams are originals and the next lapse would delete them."""
    stream, member = key(), key()

    async def go():
        node = await Node({pub(member)}, cfg={"concord_backup_relays": [closed_port_url()]}).start()
        try:
            assert (await node.publish(replicate(member, [pub(stream)])))[2] is True
            await node.mirror._save_now()
            again = cm.ConcordMirror(node.store, None, node.cfg, url_ok=lambda u, c: "", open_ws=lambda u: None)
            raw = await node.store.kv_get(cm.KV_KEY)
            assert pub(stream) in json.loads(raw)["hosted"]
            await again.start()
            assert again.is_hosted(pub(stream))
            await again.stop()
        finally:
            await node.close()
    run(go())
