#!/usr/bin/env python3
"""PosterChanDB parity harness — does it answer EXACTLY what the relay answers today?

Runs on a node against its own relay database, READ-ONLY (it never calls RelayStore.open(), which would
run schema changes). It:
  1. streams every stored event out of Postgres into a separate PosterChanDB at --dir (default
     $POSTERCHANDB_DIR/parity, default /var/lib/posterchandb/parity), plus the derived `_quote_author` tags;
  2. builds a query corpus FROM THE DATA: author feeds, profiles/follows/relay lists, mentions (with and
     without quotes), threads, hashtags, time windows, id batches, `#d~` folder reads, cursor paging and
     one- and two-word searches;
  3. runs each through the relay's own Postgres code path (RelayStore._query_one) and through
     PosterChanDB, and compares the returned ids IN ORDER;
  4. prints counts per category, timings, RAM and disk — ids and numbers only, never content.

Exit status 0 only if every category matched. Usage:
  venv-unified/bin/python scripts/posterchandb_parity.py [--dir PATH] [--queries 300] [--reuse]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import resource
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.nostr_relay import store as relay_store_mod  # noqa: E402
from app.services.posterchandb import data_dir  # noqa: E402
from app.services.posterchandb.store import Store, search_words  # noqa: E402

WORD = re.compile(r"[a-z]{4,}")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def snapshot_conn(dsn):
    """ONE read-only REPEATABLE READ transaction for the whole run: the load AND every Postgres answer it is
    compared with see the same moment. The first production run loaded at one moment and asked the LIVE
    relay 45 minutes later -- the sampled authors are the active ones, so their newest posts and profile
    replacements read as mismatches (same count, a few ids swapped) that were the relay moving, not the
    store being wrong. (A long open transaction holds back VACUUM on that database for the run's length.)"""
    import psycopg2
    raw = psycopg2.connect(dsn, connect_timeout=10)
    raw.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
    return raw


def load(raw, dst: Store, batch: int = 20000) -> int:
    """Stream events oldest first (so replaceable/deletion rules replay in order), inside the run's snapshot
    transaction (a server-side named cursor needs one anyway)."""
    n = 0
    with raw.cursor(name="pcdb_parity_load") as cur:
        cur.itersize = batch
        cur.execute("SELECT " + relay_store_mod.EVENT_COLUMNS + " FROM events e ORDER BY e.created_at, e.id")
        for row in cur:
            try:
                ev = relay_store_mod.event_from_row(row)
            except Exception:
                continue
            dst.put(ev)
            n += 1
            if n % 200000 == 0:
                dst.flush()
                print("  loaded %d events, rss %.0f MB" % (n, rss_mb()), flush=True)
    with raw.cursor() as cur:
        cur.execute("SELECT event_id, value FROM event_tags WHERE tag='_quote_author'")
        for eid, val in cur:
            dst.add_derived_tag(eid, "_quote_author", val)
    dst.flush()
    return n


def corpus(pg, k: int, rnd: random.Random) -> dict:
    raw = pg._raw
    q = defaultdict(list)
    with raw.cursor() as cur:
        cur.execute("SELECT pubkey FROM events TABLESAMPLE SYSTEM (1) LIMIT %s", (k,))
        authors = list({r[0] for r in cur})
        cur.execute("SELECT value FROM event_tags TABLESAMPLE SYSTEM (1) WHERE tag='p' LIMIT %s", (k,))
        ps = list({r[0] for r in cur})
        cur.execute("SELECT value FROM event_tags TABLESAMPLE SYSTEM (1) WHERE tag='e' LIMIT %s", (k,))
        es = list({r[0] for r in cur})
        cur.execute("SELECT value FROM event_tags TABLESAMPLE SYSTEM (2) WHERE tag='t' LIMIT %s", (k,))
        ts = list({r[0] for r in cur})
        cur.execute("SELECT value FROM event_tags TABLESAMPLE SYSTEM (5) WHERE tag='d' AND value LIKE 'pcai:%%:%%' LIMIT %s", (k,))
        ds = list({r[0].rsplit(":", 1)[0] + ":" for r in cur})
        cur.execute("SELECT id, created_at FROM events TABLESAMPLE SYSTEM (1) LIMIT %s", (k * 5,))
        idrows = list(cur)
        cur.execute("SELECT content FROM events TABLESAMPLE SYSTEM (0.5) WHERE kind=1 LIMIT %s", (k,))
        words = [w for (c,) in cur for w in WORD.findall((c or "").lower())]
        cur.execute("SELECT pubkey FROM events WHERE kind=30078 GROUP BY pubkey ORDER BY count(*) DESC LIMIT 5")
        doc_authors = [r[0] for r in cur]
    for a in authors:
        q["author_feed"].append({"authors": [a], "kinds": [1, 6], "limit": rnd.choice([20, 100, 500])})
        q["profile_lists"].append({"authors": [a], "kinds": [0, 3, 10002]})
        q["author_any"].append({"authors": [a], "limit": 50})
    for p in ps:
        q["mentions"].append({"#p": [p], "kinds": [1, 6, 7, 9735], "limit": 100})
        q["mentions_quotes"].append({"#p": [p], "kinds": [1], "_include_quotes": True, "limit": 100})
    for e in es:
        q["thread"].append({"#e": [e], "limit": 500})
    for t in ts:
        q["hashtag"].append({"#t": [t], "kinds": [1], "limit": 100})
    for d in ds:
        q["d_prefix"].append({"#d~": [d], "kinds": [30078], "limit": 500})
    rnd.shuffle(idrows)
    for i in range(0, min(len(idrows), k * 5), 50):
        q["ids"].append({"ids": [r[0] for r in idrows[i:i + 50]]})
    for _, ca in idrows[:k]:
        q["window"].append({"kinds": [1], "since": ca - 3600, "until": ca, "limit": 200})
    for w in rnd.sample(words, min(k, len(words))):
        q["search_1"].append({"search": w, "limit": 100})
    for _ in range(min(k, len(words) // 2)):
        q["search_2"].append({"search": "%s %s" % tuple(rnd.sample(words, 2)), "limit": 100})
    for a in doc_authors:
        q["cursor_pages"].append({"authors": [a], "kinds": [30078], "limit": 200})
    return q


def run_pg(rs, conn, flt):
    return [e["id"] for e in rs._query_one(conn, flt)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(data_dir(), "parity"))
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--reuse", action="store_true", help="reopen an existing --dir instead of reloading")
    ap.add_argument("--seed", type=int, default=1009)
    a = ap.parse_args()
    rs = relay_store_mod.RelayStore()
    raw = snapshot_conn(rs.dsn)
    conn = relay_store_mod._PgConn(raw)
    if not a.reuse and os.path.isdir(a.dir):
        for name in os.listdir(a.dir):
            if name.startswith("seg-") and name.endswith(".log"):
                os.remove(os.path.join(a.dir, name))
    t = time.time()
    db = Store(a.dir, flush_interval=3600, direct_durable=False, log=lambda m: print(m))
    if not a.reuse:
        print("loading from Postgres …", flush=True)
        n = load(raw, db)
        print("loaded %d events in %.0f s" % (n, time.time() - t))
    else:
        print("reopened %d events in %.0f s (NOTE: compared with Postgres as it is NOW, not as it was at the load -- expect drift on active authors)" % (len(db.off), time.time() - t))
    st = db.stats()
    disk = sum(os.path.getsize(os.path.join(a.dir, n)) for n in os.listdir(a.dir) if n.startswith("seg-"))
    print(json.dumps({"events": st["events"], "dead": st["dead"], "arena_MB": round(st["arena_bytes"] / 1e6),
                      "index_MB": round(st["index_bytes"] / 1e6), "disk_MB": round(disk / 1e6),
                      "rss_MB": round(rss_mb())}))
    rnd = random.Random(a.seed)
    q = corpus(conn, a.queries, rnd)
    now = int(time.time())
    failed = 0
    report = {}
    for cat, flts in q.items():
        ok = bad = 0
        tp = tq = 0.0
        examples = []
        for flt in flts:
            if cat == "cursor_pages":
                # walk every page through both, comparing page by page
                cur_pg = None
                while True:
                    f1 = dict(flt, **({"_cursor": cur_pg} if cur_pg else {}))
                    t0 = time.perf_counter(); p = run_pg(rs, conn, f1); tp += time.perf_counter() - t0
                    t0 = time.perf_counter(); d = [e["id"] for e in db.query(f1, now=now)]; tq += time.perf_counter() - t0
                    if p != d:
                        bad += 1; examples.append({"filter_kind": cat, "pg": len(p), "db": len(d)}); break
                    if not p:
                        ok += 1; break
                    last = rs._query_one(conn, f1)[-1]
                    cur_pg = [last["created_at"], last["id"]]
                continue
            t0 = time.perf_counter(); p = run_pg(rs, conn, flt); tp += time.perf_counter() - t0
            t0 = time.perf_counter(); d = [e["id"] for e in db.query(flt, now=now)]; tq += time.perf_counter() - t0
            if p == d:
                ok += 1
            else:
                bad += 1
                if len(examples) < 3:
                    sp, sd = set(p), set(d)
                    examples.append({"pg": len(p), "db": len(d), "only_pg": len(sp - sd), "only_db": len(sd - sp),
                                     "same_set_other_order": sp == sd,
                                     "keys": sorted(k for k in flt if k != "search"),
                                     "search_words": len(search_words(flt.get("search", "")))})
        failed += bad
        report[cat] = {"ok": ok, "mismatch": bad, "pg_ms_avg": round(tp / max(1, ok + bad) * 1000, 2),
                       "pcdb_ms_avg": round(tq / max(1, ok + bad) * 1000, 2), "examples": examples}
        print(cat, json.dumps(report[cat]), flush=True)
    db.close()
    raw.rollback()
    raw.close()
    print("TOTAL mismatches:", failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
