"""The fediverse bridge's two tables as DocTables (#161): the puppet registry and the delivered-notes ledger.

PUPPETS (`fedi_puppets`, one document per fediverse account, keyed by the canonical actor URI -- the same
primary key the SQL table had). A puppet's KEY is never stored (it is re-derived from the actor URI); the
row records which actor URI a pubkey was derived from, its handle and its last published profile. The
identity rules live in fedi_bridge_identity.ensure_puppet, which is still the ONE place a fediverse person
gets a key; what this module adds is that a registry that cannot be read is `Unavailable`, never "this
person is new" -- read as new, the alias rule (one person's /@alice and /users/alice share one key) is
skipped and a SECOND identity is minted beside the real one.

LEDGER (`fedi_bridge_delivered`, one document per handled note or DM, keyed by
sha256(platform, instance_url, note_id) -- so recording the same note twice is the same document, not a
second row). It is the dedup key that stops a note being stored twice, and it is bounded by the daily prune
(`aprune`, the relay's retention window). A ledger that cannot be read is `Unavailable`: the caller must
neither deliver (it may be a duplicate) nor record anything; it retries.

Relay-only (no SQL side; a node's old rows are copied once by scripts/migrate_tables_to_nostr.py). Puppet
lookups scan the in-memory table; the LEDGER is read by point (see "ledger" below), never loaded in whole by a
request, because at ~390k rows that load takes many minutes after a restart.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta

from app.services import table_migrate as tm
from app.services.doc_table import DocTable
from app.services.table_migrate import Row

PUPPETS = "fedi_puppets"
LEDGER = "fedi_bridge_delivered"

_P_FIELDS = {"actor_uri": "", "acct": "", "instance_host": None, "pubkey_hex": "", "nip05_name": "",
             "display_name": None, "avatar_url": None, "profile_sig": None, "last_seen": None,
             "created_at": None}
_L_FIELDS = {"platform": "", "instance_url": "", "note_id": "", "note_uri": None, "author_acct": None,
             "nostr_event_id": "", "nostr_pubkey": None, "created_at": None, "deleted_at": None}


def _prow(r: dict) -> Row:
    return Row({**_P_FIELDS, **(r or {})})


def _lrow(r: dict) -> Row:
    return Row({**_L_FIELDS, **(r or {})})


def _first(rows):
    """The earliest-created row among several (what a stable `.first()` would have meant)."""
    rows = list(rows)
    return min(rows, key=lambda r: (r.get("created_at") or "", r.get("actor_uri") or "")) if rows else None


# ============================================================================== in-memory secondary indexes
# Lookups by actor URI / handle / pubkey (puppets) and by note URI / event id (ledger) used to SCAN the whole table
# per inbox activity -- 23k puppets and 388k ledger rows, on every delivery (2026-10-10). These maps are kept
# current through DocTable's watcher (its own writes, the live stream, a (re)load) and are only consulted once the
# table is loaded; before that the callers fall back exactly as before.
_ix_lock = threading.Lock()
_pix = {"acct": {}, "pk": {}}            # lower(acct) -> {uri}; pubkey -> {uri}
_lix = {"uri": {}, "eid": {}}            # note_uri -> {key}; nostr_event_id -> {key}
_ix_seen: dict = {}                      # (table, key) -> the values indexed for it (to unindex on change)


def _ix_put(name, maps, fields, k, row):
    old = _ix_seen.pop((name, k), None)
    if old:
        for f, v in old:
            bucket = maps[f].get(v)
            if bucket is not None:
                bucket.discard(k)
                if not bucket:
                    del maps[f][v]
    if row is None:
        return
    vals = []
    for f, get in fields.items():
        v = get(row)
        if v:
            maps[f].setdefault(v, set()).add(k)
            vals.append((f, v))
    if vals:
        _ix_seen[(name, k)] = vals


_P_IX = {"acct": lambda r: (r.get("acct") or "").lower().lstrip("@"), "pk": lambda r: r.get("pubkey_hex")}
_L_IX = {"uri": lambda r: r.get("note_uri"), "eid": lambda r: r.get("nostr_event_id")}


def _watcher(name, maps, fields):
    def on_change(k, row):
        with _ix_lock:
            if k is None:
                for m in maps.values():
                    m.clear()
                for key in [x for x in _ix_seen if x[0] == name]:
                    del _ix_seen[key]
                for kk, r in DocTable(name).rows_view():
                    _ix_put(name, maps, fields, kk, r)
            else:
                _ix_put(name, maps, fields, k, row)
    return on_change


def _indexed(name) -> bool:
    """True when `name` is loaded in this process and its secondary index is current. The watcher is attached to
    the table INSTANCE (a fresh process -- or a test resetting the registry -- gets a new one)."""
    t = DocTable(name)
    if not getattr(t, "_fedi_ix_watch", False):
        with _ix_lock:
            if not getattr(t, "_fedi_ix_watch", False):
                maps, fields = (_pix, _P_IX) if name == PUPPETS else (_lix, _L_IX)
                fn = _watcher(name, maps, fields)
                t._fedi_ix_watch = True
                t.watch(fn)
                for m in maps.values():
                    m.clear()
                for key in [x for x in _ix_seen if x[0] == name]:
                    del _ix_seen[key]
                if t.loaded:
                    for kk, r in t.rows_view():
                        _ix_put(name, maps, fields, kk, r)
    return t.loaded


def puppet_pubkeys_for(actor_uri: str, acct_lower: str) -> set:
    """Puppet keys recorded for this actor URI or handle. Raises Unavailable until the registry is loaded."""
    t = DocTable(PUPPETS)
    if not _indexed(PUPPETS):
        t.peek("")                                # raises Loading/Unavailable, never answers "none"
    keys = {actor_uri} if actor_uri else set()
    with _ix_lock:
        if acct_lower:
            keys |= set(_pix["acct"].get(acct_lower, ()))
    out = set()
    for k in keys:
        r = t.peek(k)
        if r and r.get("pubkey_hex"):
            out.add(r["pubkey_hex"])
    return out


# ============================================================================================ puppets

def puppet_by_uri(uri: str) -> Row | None:
    if not uri:
        return None
    r = tm.row(PUPPETS, uri)
    return _prow(r) if r is not None else None


async def apuppet_by_uri(uri: str) -> Row | None:
    if not uri:
        return None
    r = await DocTable(PUPPETS).aget(uri)
    return _prow(r) if r is not None else None


def _by_pubkey(rows, pk):
    r = _first(r for _k, r in rows if r.get("pubkey_hex") == pk)
    return _prow(r) if r is not None else None


def puppet_by_pubkey(pk: str) -> Row | None:
    return _by_pubkey(tm.view(PUPPETS), pk) if pk else None


async def apuppet_by_pubkey(pk: str) -> Row | None:
    return _by_pubkey(await tm.aview(PUPPETS), pk) if pk else None


def _by_pubkeys(rows, pks) -> dict:
    want = {p for p in pks if p}
    out: dict = {}
    for _k, r in rows:
        pk = r.get("pubkey_hex")
        if pk in want:
            cur = out.get(pk)
            if cur is None or (r.get("created_at") or "") < (cur.get("created_at") or ""):
                out[pk] = r
    return {pk: _prow(r) for pk, r in out.items()}


def puppets_by_pubkeys(pks) -> dict:
    """{pubkey: row} for the puppets among `pks`."""
    return _by_pubkeys(tm.view(PUPPETS), pks)


async def apuppets_by_pubkeys(pks) -> dict:
    return _by_pubkeys(await tm.aview(PUPPETS), pks)


def puppets_by_uris(uris) -> dict:
    want = {u for u in uris if u}
    return {k: _prow(r) for k, r in tm.view(PUPPETS) if k in want}


def puppet_pubkeys() -> set:
    if _indexed(PUPPETS):
        with _ix_lock:
            return set(_pix["pk"])
    return {r.get("pubkey_hex") for _k, r in tm.view(PUPPETS) if r.get("pubkey_hex")}


async def apuppet_pubkeys() -> set:
    if _indexed(PUPPETS):
        with _ix_lock:
            return set(_pix["pk"])
    return {r.get("pubkey_hex") for _k, r in await tm.aview(PUPPETS) if r.get("pubkey_hex")}


def all_puppets() -> list:
    return [_prow(r) for _k, r in tm.view(PUPPETS)]


async def aall_puppets() -> list:
    return [_prow(r) for _k, r in await tm.aview(PUPPETS)]


async def apuppets_with_acct(acct: str) -> list:
    """Rows recorded under this exact handle, earliest first."""
    if acct and _indexed(PUPPETS):
        t = DocTable(PUPPETS)
        with _ix_lock:
            keys = list(_pix["acct"].get(acct.lower().lstrip("@"), ()))
        rows = [_prow(r) for r in (t.peek(k) for k in keys) if r and r.get("acct") == acct]
        return sorted(rows, key=lambda r: (r.get("created_at") or "", r.get("actor_uri") or ""))
    rows = [_prow(r) for _k, r in await tm.aview(PUPPETS) if acct and r.get("acct") == acct]
    return sorted(rows, key=lambda r: (r.get("created_at") or "", r.get("actor_uri") or ""))


async def aput_puppet(row: dict) -> None:
    """Write one puppet row (raises Unavailable when it was not stored)."""
    r = {k: row.get(k, v) for k, v in _P_FIELDS.items()}
    r["last_seen"], r["created_at"] = tm.iso(r["last_seen"]), tm.iso(r["created_at"])
    await DocTable(PUPPETS).aput(r["actor_uri"], r)


# ============================================================================================ ledger
# THE LEDGER IS READ BY POINT, NEVER BY LOADING IT (2026-10-10). It holds ~390k rows on a busy node and a whole
# load took 20 minutes after a restart -- and the inbox asked for it in whole, so every POST /ap/inbox answered
# 503 for as long as that took. Every lookup now names the documents it needs:
#   * by note URI: the row the inbox writes for a URI has a DETERMINISTIC key (`_ap_key`); a row recorded under
#     any other key (the retired Pleroma bridge's) is named by the index document `u:<sha256(uri)>`;
#   * by event id: the index document `e:<event id>` names the rows of mirrored notes (rows WITH a note URI --
#     a DM's row is bookkeeping about a wrap, never "a note from the fediverse");
#   * by (platform, instance, note id): the row's own key, as before.
# Index documents (`fedi_ledger_index`) are written BEFORE the row and dropped BEFORE it, so an index entry can
# name a row that is not there (harmless: the row is read and found absent) but a row can never be missing from
# the index -- which would be a duplicate delivery. Rows from before the index are indexed once by
# `abackfill_index` (started at app startup), which writes `_complete` last; until that marker exists every
# lookup falls back to the whole table (Loading while it loads), exactly the old behaviour.

INDEX = "fedi_ledger_index"
_INDEX_DONE = "_complete"


def ledger_key(platform: str, instance_url: str, note_id: str) -> str:
    raw = "\x1f".join([platform or "", instance_url or "", (note_id or "")[:255]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _origin(uri: str) -> str:
    """The same origin activitypub.inbox records a note under."""
    from urllib.parse import urlparse
    p = urlparse(uri or "")
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _ap_key(uri: str) -> str:
    """The key the inbox records a fediverse object's row under (inbox._record)."""
    return ledger_key("activitypub", _origin(uri), (uri or "")[:255])


def _uri_ikey(uri: str) -> str:
    return "u:" + hashlib.sha256((uri or "")[:512].encode("utf-8")).hexdigest()[:40]


def _event_ikey(eid: str) -> str:
    return "e:" + str(eid)


def _index_entries(k: str, row: dict) -> list:
    """The index documents that must name row `k`."""
    uri = row.get("note_uri")
    if not uri:
        return []
    out = []
    if k != _ap_key(uri):
        out.append(_uri_ikey(uri))
    if row.get("nostr_event_id"):
        out.append(_event_ikey(row["nostr_event_id"]))
    return out


async def _aindex_ready(*, fresh: bool = False) -> bool:
    """Whether every ledger row is in the index (the backfill's `_complete` marker). Raises Unavailable when the
    relay cannot be asked. Remembered once seen (per table instance); `fresh` re-reads it -- which is also a
    liveness check that the relay answers point reads at all."""
    t = DocTable(INDEX)
    if not fresh and getattr(t, "_ledger_index_complete", False):
        return True
    m = await t.aget(_INDEX_DONE)
    ok = bool(m and m.get("ok") is True)
    if ok:
        t._ledger_index_complete = True
    return ok


async def _aindex_add(k: str, row: dict) -> None:
    t = DocTable(INDEX)
    for ik in _index_entries(k, row):
        cur = await t.aget(ik)
        keys = list((cur or {}).get("keys") or [])
        if k not in keys:
            await t.aput(ik, {"keys": keys + [k]})


async def _aindex_drop(k: str, row: dict) -> None:
    t = DocTable(INDEX)
    for ik in _index_entries(k, row):
        cur = await t.aget(ik)
        if cur is None:
            continue
        old = list(cur.get("keys") or [])
        keys = [x for x in old if x != k]
        if not keys:
            await t.adelete(ik)
        elif keys != old:
            await t.aput(ik, {"keys": keys})


async def _arows_by_uri(uri: str) -> list:
    want = (uri or "")[:512]
    if await _aindex_ready():
        idx = await DocTable(INDEX).aget(_uri_ikey(uri))
        keys = [_ap_key(uri)] + list((idx or {}).get("keys") or [])
        items = (await DocTable(LEDGER).aget_many(keys)).items()
    elif _indexed(LEDGER):
        t = DocTable(LEDGER)
        with _ix_lock:
            keys = list(_lix["uri"].get(want, ()))
        items = [(k, r) for k, r in ((k, t.peek(k)) for k in keys) if r is not None]
    else:
        items = await tm.aview(LEDGER)
    return [(k, r) for k, r in items if r.get("note_uri") == want]


async def _arows_by_events(eids) -> list:
    ids = {e for e in eids if e}
    if not ids:
        return []
    if await _aindex_ready():
        idx = await DocTable(INDEX).aget_many([_event_ikey(e) for e in ids])
        keys = [k for d in idx.values() for k in (d.get("keys") or [])]
        items = (await DocTable(LEDGER).aget_many(keys)).items()
    elif _indexed(LEDGER):
        t = DocTable(LEDGER)
        with _ix_lock:
            keys = {k for e in ids for k in _lix["eid"].get(e, ())}
        items = [(k, r) for k, r in ((k, t.peek(k)) for k in keys) if r is not None]
    else:
        items = await tm.aview(LEDGER)
    return [(k, r) for k, r in items if r.get("nostr_event_id") in ids and r.get("note_uri") is not None]


def _earliest(items):
    rows = [r for _k, r in items]
    r = min(rows, key=lambda r: r.get("created_at") or "") if rows else None
    return _lrow(r) if r is not None else None


def _blocking(coro):
    """A synchronous lookup (the inbox's worker threads). Point reads are async, so on an event loop there is
    nothing a sync call may wait on: Unavailable, never a blocked loop."""
    from app.services import doc_table
    from app.services.relay_reader import Unavailable
    if doc_table._in_loop():
        coro.close()
        raise Unavailable("the delivered-notes ledger is read with its async API on an event loop")
    return doc_table._run(coro)


async def adelivered_by_uri(uri: str) -> Row | None:
    """The first ledger row for a note URI, or None. Raises Unavailable ("could not ask" is never "new")."""
    return _earliest(await _arows_by_uri(uri)) if uri else None


def delivered_by_uri(uri: str) -> Row | None:
    return _blocking(adelivered_by_uri(uri)) if uri else None


async def adelivered_by_event(eid: str) -> Row | None:
    """The row of the mirrored fediverse note stored as event `eid` (rows with a note URI only), or None."""
    return _earliest(await _arows_by_events([eid])) if eid else None


def delivered_by_event(eid: str) -> Row | None:
    return _blocking(adelivered_by_event(eid)) if eid else None


async def adelivered(platform: str, instance_url: str, note_id: str) -> bool:
    t = DocTable(LEDGER)
    return await t.aget(ledger_key(platform, instance_url, note_id)) is not None


async def amirrored_among(event_ids) -> set:
    """Which of these event ids are notes mirrored from the fediverse (a ledger row WITH a note URI)."""
    return {r.get("nostr_event_id") for _k, r in await _arows_by_events(event_ids)}


def mirrored_among(event_ids) -> set:
    return _blocking(amirrored_among(list(event_ids)))


async def arecord(*, platform: str, instance_url: str, note_id: str, nostr_event_id: str,
                  note_uri: str | None = None, author_acct: str | None = None,
                  nostr_pubkey: str | None = None) -> None:
    """Record a handled note/DM. Raises Unavailable when it was not stored. The index first: a row the index
    does not name would be invisible to the point lookups, i.e. delivered again."""
    row = {"platform": platform, "instance_url": instance_url or "", "note_id": (note_id or "")[:255],
           "note_uri": note_uri[:512] if note_uri else None, "author_acct": author_acct or None,
           "nostr_event_id": nostr_event_id or "", "nostr_pubkey": nostr_pubkey or None,
           "created_at": tm.iso(datetime.utcnow()), "deleted_at": None}
    k = ledger_key(platform, instance_url, note_id)
    await _aindex_add(k, row)
    await DocTable(LEDGER).aput(k, row)


def record(**kw) -> None:
    """Sync `arecord` (worker threads)."""
    from app.services.doc_table import _run
    _run(arecord(**kw))


def forget_uri(uri: str) -> int:
    from app.services.doc_table import _run
    return _run(aforget_uri(uri))


async def _adrop(k: str, row: dict) -> None:
    await _aindex_drop(k, row)
    await DocTable(LEDGER).adelete(k)


async def aforget_uri(uri: str) -> int:
    """Drop every ledger row for a note URI (its event was deleted). Raises Unavailable."""
    n = 0
    for k, r in (await _arows_by_uri(uri) if uri else []):
        await _adrop(k, r)
        n += 1
    return n


async def aprune(keep_days: int) -> int:
    """Delete rows older than `keep_days` (0 keeps everything) -- the one whole-table read left, on the worker's
    daily job. Raises Unavailable."""
    if keep_days <= 0:
        return 0
    cutoff = tm.iso(datetime.utcnow() - timedelta(days=keep_days))
    n = 0
    for k, r in await tm.aview(LEDGER):
        if (r.get("created_at") or "") < cutoff:
            await _adrop(k, r)
            n += 1
    return n


async def abackfill_index() -> int:
    """Index every row recorded before the index existed (once per node; idempotent). Reads the WHOLE ledger
    (Unavailable/Loading until it can), merges into whatever index documents writes have made since, and writes
    `_complete` LAST -- until then lookups use the whole table. Returns the index documents written."""
    if await _aindex_ready(fresh=True):
        return 0
    rows = await tm.aview(LEDGER)
    want: dict = {}
    for k, r in rows:
        for ik in _index_entries(k, r):
            want.setdefault(ik, set()).add(k)
    have = await DocTable(INDEX).aget_many(list(want))
    put = {}
    for ik, ks in want.items():
        cur = list((have.get(ik) or {}).get("keys") or [])
        merged = cur + sorted(k for k in ks if k not in cur)
        if merged != cur:
            put[ik] = {"keys": merged}
    if put:
        from app.services import doc_table_bulk
        await doc_table_bulk.abulk_put(INDEX, put)
    await DocTable(INDEX).aput(_INDEX_DONE, {"ok": True, "rows": len(rows), "entries": len(want),
                                             "at": tm.iso(datetime.utcnow())})
    DocTable(INDEX)._ledger_index_complete = True
    return len(put)


_backfill = {"thread": None}


def start_index_backfill() -> None:
    """Run `abackfill_index` on a thread of its own until it has succeeded once (port-3051 startup). Idempotent."""
    import asyncio
    import logging
    import threading
    import time
    log = logging.getLogger(__name__)
    th = _backfill["thread"]
    if th is not None and th.is_alive():
        return

    def loop():
        delay = 10.0
        while True:
            try:
                n = asyncio.run(abackfill_index())
                if n:
                    log.info("[fedi-ledger] index backfilled: %d document(s) written", n)
                return
            except Exception as e:      # noqa: BLE001 -- the ledger is loading or the relay is busy: try later
                log.info("[fedi-ledger] index backfill waits: %s", e)
            time.sleep(delay)
            delay = min(delay * 2, 300.0)

    th = threading.Thread(target=loop, name="fedi-ledger-index", daemon=True)
    _backfill["thread"] = th
    th.start()


async def aledger_ready() -> None:
    """The ledger can answer lookups: its index is complete (checked FRESH -- a relay that cannot be asked one
    document raises Unavailable here, before anything is accepted), or else the whole table is loaded."""
    if await _aindex_ready(fresh=True):
        return
    await DocTable(LEDGER)._ensure()


async def aready() -> None:
    """The puppet registry readable in WHOLE and the ledger able to answer -- raises Unavailable otherwise
    (Loading while a restart's load runs). An entry point that will make decisions from them (the inbox, the DM
    listener) asks this FIRST and refuses or waits, so that the lookups below it cannot fail half-way through a
    delivery. The ledger is NOT loaded in whole once its index is complete: its lookups are point reads."""
    await DocTable(PUPPETS)._ensure()
    await aledger_ready()
