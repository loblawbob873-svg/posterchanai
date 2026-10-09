"""PosterChanDB answers exactly what the relay's Postgres store answers — the same events in, the same filters out.

The ONE property the switch depends on. Each case feeds an identical, randomly generated event stream through
both stores' normal entry points — RelayStore._add_event_sync(ev, origin) on a throwaway Postgres (the relay's
real code, schema and SQL) and Store.put(ev, origin=…) — and then compares:

  * stored-or-refused, event by event (retired kinds, far-future and already-expired events, fedi-only actions,
    events their author already deleted, replaceable/addressable versions and every tie-break rule);
  * the answers to hundreds of random NIP-01 filters, IN ORDER (ids, authors, kinds, tags, since/until, limit,
    search, `#d~` prefix, `_cursor`, `_include_quotes`);
  * the same answers again after PosterChanDB closes and REPLAYS its log from disk.

The stream is built to hit the hard parts: equal timestamps, several origins, deletions of your own and other
people's events, `a`-coordinate deletions, several `d` tags on one document, quotes that arrive before the post
they quote, NIP-40 expirations including never-expire kinds, and search text from the tokenizer fuzzer.
"""
import asyncio
import random
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.services.nostr_relay import store as relay   # noqa: E402
from app.services.posterchandb.store import Store       # noqa: E402
from tests import scratch_postgres                       # noqa: E402
from tests.test_posterchandb import ev_id                # noqa: E402
from tests.test_posterchandb_tsparser import FRAG        # noqa: E402

DSN = scratch_postgres.dsn()
NOW = int(time.time())


def _admin():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("Postgres not reachable: %s" % e)
    conn.autocommit = True
    return conn


@pytest.fixture
def pair(tmp_path):
    schema = "pcai_pcdb_diff_" + uuid.uuid4().hex[:10]
    c = _admin()
    c.cursor().execute(f'CREATE SCHEMA "{schema}"')
    c.close()
    loop = asyncio.new_event_loop()
    rs = relay.RelayStore(DSN + f" options=-csearch_path={schema}")
    rs.open(loop)
    db = Store(str(tmp_path / "pcdb"), flush_interval=3600, direct_durable=False)
    try:
        yield rs, db, str(tmp_path / "pcdb")
    finally:
        try:
            db.close()
        except Exception:
            pass
        rs.close()
        loop.close()
        c = _admin()
        c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        c.close()


class Gen:
    def __init__(self, seed):
        self.r = random.Random(seed)
        self.authors = ["%064x" % self.r.getrandbits(256) for _ in range(6)]
        self.made = []

    def text(self):
        r = self.r
        return " ".join(r.choice(FRAG) for _ in range(r.randint(0, 8)))

    def ts(self):
        r = self.r
        return NOW - r.choice([r.randint(0, 3 * 86400), r.randint(0, 50), 1000, 1000, 2000])   # plenty of ties

    def event(self):
        r = self.r
        pk = r.choice(self.authors)
        kind = r.choice([1, 1, 1, 1, 6, 7, 0, 3, 10002, 10000, 30023, 30078, 30078, 30617, 1111, 9735, 1059, 5, 5,
                         40, 34550, 10133, 20001, 1068])
        tags = []
        if self.made and r.random() < 0.6:
            tgt = r.choice(self.made)
            tags.append(["e", tgt["id"]] + ([""] if r.random() < 0.3 else []))
        for _ in range(r.randint(0, 3)):
            tags.append(r.choice([["p", r.choice(self.authors)], ["t", r.choice(["nostr", "art", "Nostr", "x y"])],
                                  ["K", r.choice(["1", "1617"])], ["d"], ["subject", "hi"]]))
        if kind in (30023, 30078, 30617, 34550) or r.random() < 0.05:
            for _ in range(r.choice([1, 1, 1, 2])):
                tags.append(["d", r.choice(["", "a", "b", "pcai:note:1", "pcai:note:2", "pcai:mail:x:INBOX:1"])])
        if kind == 5 and self.made:
            tgt = r.choice(self.made)
            if r.random() < 0.5:
                tags.append(["e", tgt["id"]])
            else:
                d = next((t[1] for t in tgt["tags"] if len(t) >= 2 and t[0] == "d"), "")
                tags.append(["a", "%d:%s:%s" % (tgt["kind"], r.choice([tgt["pubkey"], pk]), d)])
        if kind == 1 and self.made and r.random() < 0.2:
            q = r.choice(self.made)
            tags.append(["q", q["id"]] + ([""] if r.random() < 0.5 else ["", q["pubkey"]]))
        if r.random() < 0.08:
            tags.append(["expiration", str(r.choice([NOW - 10, NOW + 3600, NOW + 86400]))])
        if r.random() < 0.02:
            tags.append(["client-mode", "fedi-only"])
        ev = {"pubkey": pk, "created_at": r.choice([self.ts(), self.ts(), NOW + 5000]) if r.random() < 0.03
              else self.ts(), "kind": kind, "tags": tags, "content": self.text(), "sig": "%0128x" % r.getrandbits(512)}
        ev["id"] = ev_id(ev)
        self.made.append(ev)
        return ev

    def flt(self):
        r = self.r
        f = {}
        pick = r.random()
        if pick < 0.15 and self.made:
            f["ids"] = [r.choice(self.made)["id"] for _ in range(r.randint(1, 6))]
        if r.random() < 0.5:
            f["authors"] = r.sample(self.authors, r.randint(1, 3))
        if r.random() < 0.5:
            f["kinds"] = r.sample([0, 1, 3, 5, 6, 7, 1111, 9735, 10002, 30023, 30078, 1059], r.randint(1, 3))
        if r.random() < 0.2 and self.made:
            f["#e"] = [r.choice(self.made)["id"]]
        if r.random() < 0.2:
            f["#p"] = [r.choice(self.authors)]
            if r.random() < 0.5:
                f["_include_quotes"] = True
        if r.random() < 0.1:
            f["#t"] = [r.choice(["nostr", "art", "Nostr", "x y"])]
        if r.random() < 0.1:
            f["#d"] = [r.choice(["", "a", "b", "pcai:note:1"])]
        if r.random() < 0.08:
            f["#d~"] = [r.choice(["pcai:", "pcai:note:", "pcai:mail:x:INBOX:", "a"])]
        if r.random() < 0.15:
            f["search"] = r.choice([self.text(), r.choice(FRAG), " ".join(r.sample(FRAG, 2))])
        if r.random() < 0.3:
            f["since"] = NOW - r.randint(0, 3 * 86400)
        if r.random() < 0.3:
            f["until"] = NOW - r.randint(0, 2 * 86400)
        if r.random() < 0.7:
            f["limit"] = r.choice([1, 5, 20, 100, 500, 5000, 0])
        if r.random() < 0.1 and self.made:
            e = r.choice(self.made)
            f["_cursor"] = [e["created_at"], e["id"]]
        return f


ORIGINS = ["direct", "wot", "wot", "bridge", "ancestor"]


def _feed(rs, db, gen, n):
    refused = []
    for _ in range(n):
        ev = gen.event()
        origin = gen.r.choice(ORIGINS)
        a = rs._add_event_sync(dict(ev), origin)
        b = db.put(dict(ev), origin=origin)
        if a != (b == "stored"):
            refused.append({"kind": ev["kind"], "relay_stored": a, "pcdb": b, "origin": origin,
                            "tags": [t[0] for t in ev["tags"]]})
    return refused


def _compare(rs, db, gen, n):
    conn = rs._conn()
    bad = []
    for _ in range(n):
        f = gen.flt()
        want = [e["id"] for e in rs._query_one(conn, dict(f))]
        got = [e["id"] for e in db.query(dict(f), now=int(time.time()))]
        if want != got:
            bad.append({"filter_keys": sorted(f), "relay": len(want), "pcdb": len(got),
                        "same_set": set(want) == set(got)})
    return bad


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_same_events_in_same_answers_out(pair, seed):
    rs, db, path = pair
    gen = Gen(seed)
    refused = _feed(rs, db, gen, 900)
    assert not refused, "%d stored/refused decisions differ; first: %r" % (len(refused), refused[:3])
    bad = _compare(rs, db, gen, 400)
    assert not bad, "%d of 400 filters answer differently; first: %r" % (len(bad), bad[:5])
    db.close()
    db2 = Store(path)
    try:
        bad = _compare(rs, db2, gen, 200)
        assert not bad, "after a replay from disk, %d of 200 filters differ; first: %r" % (len(bad), bad[:5])
    finally:
        db2.close()


# ---------------------------------------------------------------- auto-clean: the same survivors
class OldGen(Gen):
    def ts(self):
        r = self.r
        return NOW - r.choice([r.randint(0, 400 * 86400), r.randint(0, 40 * 86400), r.randint(0, 3 * 86400)])


def _survivors_relay(rs):
    return {r["id"] for r in rs._conn().execute("SELECT id FROM events").fetchall()}


def _survivors_pcdb(db):
    out = set()
    for s in range(len(db.off)):
        if not db.dead[s] and db.seg[s] != 0xFFFFFFFF:
            out.add(db._id_hex(s))
    return out


@pytest.mark.parametrize("seed,max_events", [(11, 0), (12, 250)])
def test_auto_clean_leaves_exactly_what_the_relays_prune_leaves(pair, seed, max_events):
    from app.services.posterchandb import maintenance as M
    rs, db, _ = pair
    gen = OldGen(seed)
    assert not _feed(rs, db, gen, 900)
    assert _survivors_relay(rs) == _survivors_pcdb(db), "the stores already disagree before cleaning"
    local = gen.authors[0]
    rs.retention_days, rs.max_events = 30, max_events
    rs.preserve_pubkeys = frozenset([local])
    removed, _more = rs._prune_sync(0)
    th = M.Throttle(io_mb_s=1000, cpu_pct=100, busy_load=0, sleep=lambda s: None)
    m = M.Maintainer(db, lambda: M.Policy(retention_days=30, max_events=max_events, preserve_pubkeys=[local],
                                          min_free_pct=0), throttle=th, now=lambda: int(time.time()))
    m.run_pass()
    want, got = _survivors_relay(rs), _survivors_pcdb(db)
    assert removed > 20, "the history must give the cleaner real work, or this proves little"
    assert want == got, ("relay kept %d, PosterChanDB kept %d; only relay: %d, only pcdb: %d"
                         % (len(want), len(got), len(want - got), len(got - want)))
