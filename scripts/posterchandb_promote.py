#!/usr/bin/env python3
"""Promote a node's PosterChanDB mirror to the relay's ONLY store (POSTERCHANDB_MODE=primary, task #161).

RUN IT WITH THE RELAY STOPPED. It is READ-ONLY against Postgres and copies nothing that already matches:

  1. The mirror directory must carry a CLEAN marker (written only by a clean stop of a mirror in shadow/serve
     mode; a running relay has removed it), and its token must be the one Postgres holds (relay_kv
     `posterchandb_clean_token`) -- the proof that the directory is THIS database's state and not an older copy.
  2. Postgres's queryable event count (expiration NULL or in the future) must EQUAL the directory's live count,
     and (unless --count-only) the two id sets must be identical. A difference is printed as ids, kinds,
     origins and times -- never content.
  3. Only then are relay_kv, wot, bridge_nip05 and bridge_puppet exported into the sidecar files beside the
     events (pcdb_store.Sidecar, atomic writes) and the PRIMARY marker written. The events need no re-copy:
     the mirror already holds them.

Then set POSTERCHANDB_MODE=primary (or posterchandb_mode) and start the relay. Until it starts in primary mode,
nothing has changed: a start in serve mode retires the promotion (the sidecar would be stale) and needs a new run.

  --new   initialise an EMPTY primary store for a node that never had Postgres (no checks, nothing exported).

Exit status: 0 promoted, 2 refused (the reason is printed; nothing was written), 1 an unexpected error.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

REFUSED, PROMOTED, ERROR = 2, 0, 1


class Refused(Exception):
    pass


def _pg(dsn: str):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(dsn, connect_timeout=10)
    # Read-only and ONE snapshot: every number below describes the same instant of the database.
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
    return conn, psycopg2.extras


def _sorted_ids(raw: bytes) -> np.ndarray:
    """32-byte ids as an (N, 4) array of big-endian words in id order (lexsort; no bytes object per id)."""
    w = np.frombuffer(raw, dtype=">u8").reshape(-1, 4)
    return w[np.lexsort((w[:, 3], w[:, 2], w[:, 1], w[:, 0]))]


def _live_ids(st, now: int) -> np.ndarray:
    """The directory's queryable events, sorted (the mirror's own _live_count rule)."""
    with st._lock:
        n = len(st.off)
        dead = np.frombuffer(st.dead, dtype=np.uint8)[:n].copy()
        exp = np.frombuffer(st.expires, dtype=np.uint64)[:n].copy()
        ids = bytes(st.ids[:n * 32])
    live = (dead == 0) & ((exp == 0) | (exp > now))
    return _sorted_ids(np.frombuffer(ids, dtype=np.uint8).reshape(-1, 32)[live].tobytes())


def promote(path: str, dsn: str, *, count_only: bool = False, out=print) -> dict:
    from app.services.nostr_relay import pcdb_store
    from app.services.nostr_relay.store import MIRROR_TOKEN_KEY
    from app.services.posterchandb.store import Store

    if not os.path.isdir(path):
        raise Refused("%s does not exist — run the relay in serve mode first so the mirror copies Postgres" % path)
    if pcdb_store.read_marker(path) is not None:
        raise Refused("%s is already promoted (a %s marker exists)" % (path, pcdb_store.MARKER))
    clean = pcdb_store.read_clean_token(path)
    if clean is None:
        raise Refused("%s has no CLEAN marker: the relay is running, or its mirror did not stop cleanly. Stop the "
                      "relay (a clean stop writes CLEAN) and run this again" % path)

    conn, extras = _pg(dsn)
    try:
        cur = conn.cursor(cursor_factory=extras.DictCursor)
        cur.execute("SELECT value FROM relay_kv WHERE key=%s", (MIRROR_TOKEN_KEY,))
        row = cur.fetchone()
        token = row["value"] if row else None
        if token != clean:
            raise Refused("the directory's CLEAN token is not the one Postgres holds (directory %r, Postgres %r): it "
                          "is an older copy, or the relay has started since. Start the relay in serve mode, let it "
                          "copy, stop it cleanly, and run this again" % (clean[:20], (token or "")[:20]))
        now = int(time.time())
        cur.execute("SELECT count(*) AS n FROM events WHERE expiration IS NULL OR expiration > %s", (now,))
        pg_count = int(cur.fetchone()["n"])

        st = Store(path, flush_interval=3600)
        try:
            mine = _live_ids(st, now)
        finally:
            st.close(take_snapshot=False)
        out("postgres: %d queryable events; directory: %d" % (pg_count, len(mine)))
        if pg_count != len(mine):
            _explain(cur, mine, now, out)
            raise Refused("event counts differ (postgres %d, directory %d)" % (pg_count, len(mine)))
        if not count_only:
            named = conn.cursor(name="pcdb_promote_ids")
            named.itersize = 50000
            named.execute("SELECT id FROM events WHERE expiration IS NULL OR expiration > %s", (now,))
            theirs = _sorted_ids(b"".join(bytes.fromhex(r[0]) for r in named))
            named.close()
            if theirs.shape != mine.shape or not bool(np.array_equal(theirs, mine)):
                _explain(cur, mine, now, out)
                raise Refused("the counts match but the event ids do not")
            out("event ids identical")

        cur.execute("SELECT key, value FROM relay_kv")
        kv = {r["key"]: r["value"] for r in cur.fetchall() if r["key"] != MIRROR_TOKEN_KEY}
        cur.execute("SELECT pubkey, depth, added_at FROM wot")
        wot = {r["pubkey"]: [int(r["depth"]), int(r["added_at"])] for r in cur.fetchall()}
        cur.execute("SELECT name, pubkey FROM bridge_nip05")
        nip05 = {r["name"]: r["pubkey"] for r in cur.fetchall()}
        cur.execute("SELECT pubkey FROM bridge_puppet")
        puppets = [r["pubkey"] for r in cur.fetchall()]
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:      # noqa: BLE001
            pass

    if pcdb_store.read_clean_token(path) != clean:
        raise Refused("the CLEAN marker changed while this ran — the relay started. Nothing was written")
    pcdb_store.Sidecar(path).replace_all(kv, wot, nip05, puppets)
    summary = {"state": "promoted", "clean_token": clean, "events": pg_count, "kv": len(kv), "wot": len(wot),
               "bridge_nip05": len(nip05), "bridge_puppet": len(puppets), "promoted_at": int(time.time())}
    pcdb_store.write_marker(path, summary)
    out("promoted: %s" % {k: v for k, v in summary.items() if k != "clean_token"})
    return summary


def _explain(cur, mine: np.ndarray, now: int, out) -> None:
    """Which events differ: ids, kinds, origins, times -- never content."""
    cur.execute("SELECT id, kind, origin, created_at FROM events WHERE expiration IS NULL OR expiration > %s",
                (now,))
    pg_rows = {r["id"]: (r["kind"], r["origin"], r["created_at"]) for r in cur.fetchall()}
    mine_hex = {row.tobytes().hex() for row in mine}
    only_pg = [i for i in pg_rows if i not in mine_hex]
    only_me = [i for i in mine_hex if i not in pg_rows]
    out("only in Postgres: %d, only in the directory: %d" % (len(only_pg), len(only_me)))
    for i in only_pg[:10]:
        k, o, c = pg_rows[i]
        out("  only in Postgres: %s kind=%s origin=%s created=%s" % (i[:16], k, o, c))
    for i in only_me[:10]:
        out("  only in the directory: %s" % i[:16])


def main(argv=None) -> int:
    from app.services import posterchandb
    from app.services.nostr_relay import store as relay_store
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--path", default=os.path.join(posterchandb.data_dir(), "relay"))
    ap.add_argument("--dsn", default=relay_store._DEFAULT_DSN,
                    help="the relay's Postgres (default: $NOSTR_RELAY_PG_DSN, else the relay's built-in default)")
    ap.add_argument("--count-only", action="store_true", help="skip the id-by-id comparison")
    ap.add_argument("--new", action="store_true", help="initialise an EMPTY primary store (no Postgres)")
    a = ap.parse_args(argv)
    try:
        if a.new:
            from app.services.nostr_relay import pcdb_store
            pcdb_store.init_new(a.path)
            print("initialised an empty primary store at %s" % a.path)
            return PROMOTED
        promote(a.path, a.dsn, count_only=a.count_only)
        return PROMOTED
    except Refused as e:
        print("REFUSED: %s" % e, file=sys.stderr)
        return REFUSED
    except Exception as e:      # noqa: BLE001
        from app.services.nostr_relay.pcdb_store import PrimaryNotReady
        if isinstance(e, PrimaryNotReady):
            print("REFUSED: %s" % e, file=sys.stderr)
            return REFUSED
        print("ERROR: %r" % (e,), file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
