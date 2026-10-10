"""The one-time copy of a node's old Postgres rows into DocTables (#161) -- the engine of
scripts/migrate_tables_to_nostr.py, run by hand on the two nodes that ever had SQL data. NOTHING at runtime imports
it: the app is relay-only from the first request.

`amigrate_rows(table, rows)`: copy what differs, remove documents no SQL row accounts for, re-read strictly,
compare row for row (MigrationMismatch, no marker, on any difference), record the id high-water mark and the
`_migrated` row. Idempotent and resumable.

Writes are pipelined over ONE authenticated socket (a window of OKs outstanding), not one round trip per
document: measured, ~1.4 ms per row against the shipped relay, so 100k rows copy in a few minutes.
"""
import asyncio
import json
import logging
import time

from app.services import nostr_store
from app.services.doc_table import MARKERS, DocTable, KIND, _norm, _operator, key
from app.services.relay_reader import Unavailable, relay_port

logger = logging.getLogger(__name__)

MARKER_TABLE = MARKERS
SEQ_TABLE = "_seq"
_WINDOW = 200


class MigrationMismatch(RuntimeError):
    """The relay does not hold exactly what SQL holds -- no marker is written."""


async def aread_all(table: str) -> dict:
    """{key: row} straight from the relay, strictly (every document or an exception)."""
    sk, _pk = _operator()
    prefix = DocTable(table).prefix
    try:
        docs = await nostr_store.list_all_docs(relay_port(), prefix, seckey=sk)
    except Exception as e:      # noqa: BLE001
        raise Unavailable("could not read %s: %s" % (table, e)) from e
    import urllib.parse
    return {urllib.parse.unquote(d[len(prefix):]): v for d, v in docs.items() if isinstance(v, dict)}


async def abulk_put(table: str, rows: dict, *, window: int = _WINDOW) -> int:
    """Write {key: row} as documents of `table`, pipelined; raises on the first refusal. Returns the count."""
    import websockets
    from app.services.nostr import nip44
    from app.services.nostr.event import build_event
    if not rows:
        return 0
    sk, _pk = _operator()
    prefix = DocTable(table).prefix
    items = list(rows.items())
    url = "ws://127.0.0.1:%d/relay" % relay_port()

    def seal(chunk):
        out = []
        for k, row in chunk:
            body = nip44.encrypt_self(sk, json.dumps(row, separators=(",", ":"), default=str))
            out.append(build_event(sk, KIND, body, tags=[["d", prefix + key(k)]]))
        return out

    done = 0
    async with websockets.connect(url, open_timeout=10, close_timeout=2, max_size=None) as ws:
        # NIP-42 first: 30078 writes by the operator are accepted from an authenticated socket.
        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if msg and msg[0] == "AUTH":
            auth = build_event(sk, 22242, "", tags=[["relay", url], ["challenge", str(msg[1])]])
            await ws.send(json.dumps(["AUTH", auth]))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if m and m[0] == "OK" and m[1] == auth["id"]:
                    if not m[2]:
                        raise Unavailable("relay refused AUTH: %s" % (m[3] if len(m) > 3 else ""))
                    break
        for i in range(0, len(items), window):
            evs = await asyncio.to_thread(seal, items[i:i + window])
            want = {e["id"] for e in evs}
            for e in evs:
                await ws.send(json.dumps(["EVENT", e]))
            while want:
                m = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if m and m[0] == "OK" and m[1] in want:
                    if not m[2]:
                        raise Unavailable("relay refused a %s document: %s" % (table, m[3] if len(m) > 3 else ""))
                    want.discard(m[1])
            done += len(evs)
    return done


async def amarker(table: str):
    """The table's `_migrated` record (what the copy script wrote), or None. Strict."""
    try:
        m = await DocTable(MARKER_TABLE).aget_remote(table)
    except Unavailable as e:
        raise Unavailable("could not read the migration marker for %s: %s" % (table, e)) from e
    return m if isinstance(m, dict) else None


async def _write_marker(table: str, info: dict) -> None:
    await DocTable(MARKER_TABLE).aput(table, _norm(info))


async def amigrate_rows(table: str, rows: dict, *, extra: dict | None = None) -> dict:
    """Copy `rows` ({key: row}, the COMPLETE old SQL table) into DocTable `table`, verify, mark.

      1. COPY what the relay lacks or holds differently (a re-run after an interruption writes only that), and
         remove documents of this table that no SQL row accounts for (left by an interrupted run);
      2. RE-READ the table strictly from the relay and compare row for row: the same keys, the same content --
         anything else raises MigrationMismatch and no marker is written;
      3. record the integer-id high-water mark in `_seq` (new ids continue above every copied id) and write the
         `_migrated` record. A table already marked is copied and verified again (idempotent: nothing differs,
         nothing is written) -- the record is informational; nothing at runtime reads it."""
    t0 = time.time()
    want = {str(k): _norm(v) for k, v in rows.items()}
    sk, _pk = _operator()
    prefix = DocTable(table).prefix
    have = await aread_all(table)
    to_write = {k: v for k, v in want.items() if have.get(k) != v}
    stale = [k for k in have if k not in want]
    written = await abulk_put(table, to_write)
    for k in stale:
        if not await nostr_store.delete_doc(relay_port(), sk, prefix + key(k), strict=True):
            raise Unavailable("could not remove a stale %s document %s" % (table, k))
    t_copy = time.time() - t0
    got = await aread_all(table)
    missing = [k for k in want if k not in got]
    extra_keys = [k for k in got if k not in want]
    differ = [k for k in want if k in got and got[k] != want[k]]
    if missing or extra_keys or differ:
        raise MigrationMismatch("%s: SQL %d row(s), relay %d -- %d missing, %d extra, %d differ (e.g. %s)"
                                % (table, len(want), len(got), len(missing), len(extra_keys), len(differ),
                                   sorted(missing + extra_keys + differ)[:3]))
    ids = [int(k) for k in want if str(k).isdigit()]
    if ids and len(ids) == len(want):
        seq = DocTable(SEQ_TABLE)
        cur = await seq.aget_remote(table) or {}
        if int(cur.get("n") or 0) < max(ids):
            await seq.aput(table, {"n": max(ids)})
    info = {"ok": True, "verified": True, "rows": len(want), "at": int(time.time()), "written": written,
            "removed_stale": len(stale), "copy_s": round(t_copy, 1), "total_s": round(time.time() - t0, 1)}
    info.update(extra or {})
    await _write_marker(table, info)
    logger.info("[migrate] %s: %d row(s) verified on the relay (%d written, %d stale removed) in %.1fs",
                table, len(want), written, len(stale), time.time() - t0)
    return dict(info, table=table)
