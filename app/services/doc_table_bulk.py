"""Bulk copy of SQL rows into a DocTable, verified, with a marker -- the migration half of #161.

One generic routine, `amigrate_rows(table, rows)` -- the ONE engine every #161 table migrates through
(app/services/table_migration.py is the registry) -- so every table's migration keeps the same promises:

  * COPY: rows already on the relay with identical content are not rewritten (a re-run after an interruption
    writes only what is missing or different); documents no SQL row accounts for are removed;
  * VERIFY against a FRESH SQL read: until the marker exists SQL is the store of record and the app goes on
    writing to it (and then to the relay) while the copy runs, so the snapshot that was copied is not the
    truth to compare with. Both sides are re-read strictly; a key that differs is settled with a point read
    of each side, and SQL's value is written to the relay until they agree;
  * MARK: only then `DocTable("_migrated")` row `<table>` = {"ok": true, "verified": true, ...}. No marker means
    the app keeps reading SQL -- a partial copy is never mistaken for the table.

Writes are pipelined over ONE authenticated socket (a window of OKs outstanding), not one round trip per
document: measured, ~1.4 ms per row against the shipped relay, so 100k rows copy in a few minutes.
"""
import asyncio
import json
import logging
import time

from app.services import nostr_store
from app.services import doc_table
from app.services.doc_table import DocTable, KIND, _norm, _operator, key
from app.services.relay_reader import Unavailable, relay_port

logger = logging.getLogger(__name__)

MARKER_TABLE = doc_table.MARKERS
SEQ_TABLE = "_seq"
_WINDOW = 200
_ROUNDS = 3


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


_seen = doc_table._marked     # tables whose verified marker this process has seen (monotonic; shared)


async def ais_migrated(table: str) -> bool:
    """Has `table` been copied and verified? "Could not ask" answers False (the caller then reads SQL); once True
    it is never asked again in this process."""
    try:
        return await doc_table.amarked(table)
    except Unavailable:
        return False


def is_migrated(table: str) -> bool:
    """Synchronous `ais_migrated` (False when it cannot be known here, unless already seen)."""
    try:
        return doc_table.marked(table)
    except (Unavailable, RuntimeError):
        return False


async def amarker(table: str):
    try:
        m = await DocTable(MARKER_TABLE).aget_remote(table)
    except Unavailable as e:
        raise Unavailable("could not read the migration marker for %s: %s" % (table, e)) from e
    return m if isinstance(m, dict) else None


async def _write_marker(table: str, info: dict) -> None:
    await DocTable(MARKER_TABLE)._relay_put(table, _norm(info))
    doc_table._saw_marked(table)


def _diff(a: dict, b: dict) -> list:
    return [k for k in set(a) | set(b) if a.get(k) != b.get(k)]


async def amigrate_rows(table: str, rows, *, extra: dict | None = None, point=None,
                        rounds: int = _ROUNDS, in_thread: bool = True, before_mark=None) -> dict:
    """Copy a SQL table into DocTable `table`, verify, mark. Idempotent: an existing verified marker returns at
    once. Raises MigrationMismatch (no marker) when the relay cannot be made to hold exactly what SQL holds.

    `rows` is the complete SQL table as {key: row}, or a CALLABLE returning it afresh (sync; run on a thread).
    SQL is authoritative until the marker exists and the app keeps WRITING to it (and to the relay) meanwhile,
    so the verification compares the relay with a FRESH SQL read, not with the snapshot that was copied:
      1. copy every row the relay lacks or holds differently; remove documents no SQL row accounts for;
      2. re-read BOTH strictly. A key where they differ is settled by reading that one row on each side
         (`point(k)` for SQL; a write in flight lands within milliseconds) and, where SQL still differs, by
         writing SQL's value to the relay -- repeated until the two agree;
      3. only a round that ends with every key agreeing writes the marker. A relay that keeps refusing or
         keeps answering something else exhausts `rounds` and raises: no marker, the table stays on SQL.

    `before_mark` (async, optional) is asked once the copy has verified and before the marker is written; a False
    answer raises MigrationMismatch. A table whose store writes the relay FIRST and SQL after (table_gate, wave 2)
    copies a snapshot and uses it to re-read SQL: a row that changed after the snapshot was read may have been
    copied over by the older value, and must not be marked."""
    t0 = time.time()
    m = await amarker(table)
    if doc_table.is_marker(m):
        doc_table._saw_marked(table)
        return dict(m, table=table, skipped=True)
    reader = rows if callable(rows) else (lambda: rows)

    async def call(fn, *a):
        # SQL reads run on a thread (a big table must not hold the loop); a caller whose session is bound to
        # its own thread (an in-memory SQLite in a test) passes in_thread=False
        return await asyncio.to_thread(fn, *a) if in_thread else fn(*a)

    async def read_sql() -> dict:
        got = await call(reader)
        return {str(k): _norm(v) for k, v in got.items()}

    async def sql_one(k):
        if point is not None:
            v = await call(point, k)
            return _norm(v) if v is not None else None
        return (await read_sql()).get(k)

    t = DocTable(table)
    sk, _pk = _operator()
    prefix = t.prefix

    async def relay_delete(k):
        if not await nostr_store.delete_doc(relay_port(), sk, prefix + key(k), strict=True):
            raise Unavailable("could not remove a stale %s document %s" % (table, k))

    want = await read_sql()
    have = await aread_all(table)
    to_write = {k: v for k, v in want.items() if have.get(k) != v}
    stale = [k for k in have if k not in want]
    written = await abulk_put(table, to_write)
    for k in stale:
        await relay_delete(k)
    t_copy = time.time() - t0
    settled = fixed = 0
    for rnd in range(1, rounds + 1):
        fresh = await read_sql()
        got = await aread_all(table)
        diff = _diff(fresh, got)
        unsettled = []
        for k in diff:
            ok = False
            for _attempt in range(3):
                s = await sql_one(k)
                r = await t.aget_remote(k)
                if s == r:
                    ok = True
                    break
                if s is None:
                    await relay_delete(k)
                else:
                    await abulk_put(table, {k: s})
                fixed += 1
                await asyncio.sleep(0.05)
            if ok:
                settled += 1
            else:
                unsettled.append(k)
        if not unsettled:
            want = fresh
            break
    else:
        raise MigrationMismatch("%s: %d row(s) never agreed with SQL after %d round(s), e.g. %s"
                                % (table, len(unsettled), rounds, sorted(unsettled)[:3]))
    if before_mark is not None and not await before_mark():
        raise MigrationMismatch("%s: the SQL table changed while it was being copied" % table)
    info = {"ok": True, "verified": True, "rows": len(want), "at": int(time.time()), "written": written,
            "removed_stale": len(stale), "settled": settled, "fixed": fixed, "rounds": rnd,
            "copy_s": round(t_copy, 1), "total_s": round(time.time() - t0, 1)}
    info.update(extra or {})
    ids = [int(k) for k in want if str(k).isdigit()]
    if ids and len(ids) == len(want):
        # integer ids: where `next_id` continues once the relay is authoritative (never below an id SQL handed
        # out -- a deleted row's id is not reused, as a SQL sequence never reuses one)
        seq = DocTable(SEQ_TABLE)
        cur = await seq.aget_remote(table) or {}
        if int(cur.get("n") or 0) < max(ids):
            await seq._relay_put(table, {"n": max(ids)})
    await _write_marker(table, info)
    logger.info("[migrate] %s: %d row(s) verified on the relay (%d written, %d stale removed, %d fixed) in %.1fs",
                table, len(want), written, len(stale), fixed, time.time() - t0)
    return dict(info, table=table)
