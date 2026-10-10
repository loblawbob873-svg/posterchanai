"""Bulk copy of SQL rows into a DocTable, verified, with a marker -- the migration half of #161.

One generic routine, `amigrate_rows(table, rows)`, so every table's migration keeps the same three promises:

  * COPY: rows already on the relay with identical content are not rewritten (a re-run after an interruption
    writes only what is missing or different); documents of this table that no SQL row accounts for are left
    by an earlier, interrupted run (nothing else writes the table before the marker exists) and are removed;
  * VERIFY: the table is RE-READ from the relay, strictly and independently of anything held in memory, and
    must match the SQL rows exactly -- the same count and the same content, row for row;
  * MARK: only then `DocTable("_migrated")` row `<table>` = {"verified": true, ...}. No marker means the
    converted code keeps refusing (or reading SQL) -- a partial copy is never mistaken for the table.

Writes are pipelined over ONE authenticated socket (a window of OKs outstanding), not one round trip per
document: measured, ~1.4 ms per row against the shipped relay, so 100k rows copy in a few minutes.
"""
import asyncio
import json
import logging
import time

from app.services import nostr_store
from app.services.doc_table import DocTable, KIND, _operator, key
from app.services.relay_reader import Unavailable, relay_port

logger = logging.getLogger(__name__)

MARKER_TABLE = "_migrated"
_WINDOW = 200


class MigrationMismatch(RuntimeError):
    """The relay does not hold exactly what SQL holds -- no marker is written."""


def _norm(row: dict) -> dict:
    return json.loads(json.dumps(row, default=str))


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


_seen: set = set()        # tables whose verified marker this process has seen (monotonic)


async def ais_migrated(table: str) -> bool:
    """Has `table` been copied and verified? "Could not ask" answers False (the caller then refuses or
    reads SQL); once True it is never asked again in this process."""
    if table in _seen:
        return True
    try:
        m = await DocTable(MARKER_TABLE).aget(table)
    except Unavailable:
        return False
    if isinstance(m, dict) and m.get("verified"):
        _seen.add(table)
    return table in _seen


def is_migrated(table: str) -> bool:
    """Synchronous `ais_migrated` -- off the event loop only (False on one, unless already seen)."""
    if table in _seen:
        return True
    try:
        m = DocTable(MARKER_TABLE).get(table)
    except (Unavailable, RuntimeError):
        return False
    if isinstance(m, dict) and m.get("verified"):
        _seen.add(table)
    return table in _seen


async def amarker(table: str):
    sk, _pk = _operator()
    try:
        m = await nostr_store.get_doc(relay_port(), DocTable(MARKER_TABLE).prefix + key(table), seckey=sk,
                                      strict=True)
    except Exception as e:      # noqa: BLE001
        raise Unavailable("could not read the migration marker for %s: %s" % (table, e)) from e
    return m if isinstance(m, dict) else None


async def amigrate_rows(table: str, rows: dict, *, extra: dict | None = None, before_mark=None) -> dict:
    """Copy `rows` ({key: row}, the complete SQL table) into DocTable `table`, verify, mark. Idempotent: an
    existing verified marker returns at once. Raises MigrationMismatch (no marker) when the re-read differs.

    `before_mark` (async, optional) is asked once the copy has verified and before the marker is written; a False
    answer raises MigrationMismatch. A table that stays writable during the copy uses it to re-read SQL: a row
    that changed after `rows` was read may have been copied over by the older value, and must not be marked."""
    t0 = time.time()
    m = await amarker(table)
    if m and m.get("verified"):
        _seen.add(table)
        return {"table": table, "skipped": True, "marker": m}
    want = {str(k): _norm(v) for k, v in rows.items()}
    have = await aread_all(table)
    to_write = {k: v for k, v in want.items() if have.get(k) != v}
    stale = [k for k in have if k not in want]
    written = await abulk_put(table, to_write)
    sk, _pk = _operator()
    prefix = DocTable(table).prefix
    for k in stale:
        if not await nostr_store.delete_doc(relay_port(), sk, prefix + key(k), strict=True):
            raise Unavailable("could not remove a stale %s document %s" % (table, k))
    t_copy = time.time() - t0
    got = await aread_all(table)
    if len(got) != len(want):
        raise MigrationMismatch("%s: SQL has %d rows, the relay holds %d" % (table, len(want), len(got)))
    bad = [k for k, v in want.items() if got.get(k) != v]
    if bad:
        raise MigrationMismatch("%s: %d row(s) differ after the copy, e.g. %s" % (table, len(bad), bad[:3]))
    if before_mark is not None and not await before_mark():
        raise MigrationMismatch("%s: the SQL table changed while it was being copied" % table)
    info = {"verified": True, "rows": len(want), "at": int(time.time()), "written": written,
            "removed_stale": len(stale), "copy_s": round(t_copy, 1), "total_s": round(time.time() - t0, 1)}
    info.update(extra or {})
    await DocTable(MARKER_TABLE).aput(table, info)
    _seen.add(table)
    logger.info("[migrate] %s: %d row(s) verified on the relay (%d written, %d stale removed) in %.1fs",
                table, len(want), written, len(stale), time.time() - t0)
    return dict(info, table=table)
