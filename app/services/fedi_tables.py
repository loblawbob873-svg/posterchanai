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

Relay-only (no SQL side; a node's old rows are copied once by scripts/migrate_tables_to_nostr.py). Lookups
scan the in-memory table -- the indexes the SQL table had are what these scans replace -- and a point read
(`apuppet_by_uri`, `adelivered`) reads one document while a restart's load is still running.
"""
from __future__ import annotations

import hashlib
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
    return {r.get("pubkey_hex") for _k, r in tm.view(PUPPETS) if r.get("pubkey_hex")}


async def apuppet_pubkeys() -> set:
    return {r.get("pubkey_hex") for _k, r in await tm.aview(PUPPETS) if r.get("pubkey_hex")}


def all_puppets() -> list:
    return [_prow(r) for _k, r in tm.view(PUPPETS)]


async def aall_puppets() -> list:
    return [_prow(r) for _k, r in await tm.aview(PUPPETS)]


async def apuppets_with_acct(acct: str) -> list:
    """Rows recorded under this exact handle, earliest first."""
    rows = [_prow(r) for _k, r in await tm.aview(PUPPETS) if acct and r.get("acct") == acct]
    return sorted(rows, key=lambda r: (r.get("created_at") or "", r.get("actor_uri") or ""))


async def aput_puppet(row: dict) -> None:
    """Write one puppet row (raises Unavailable when it was not stored)."""
    r = {k: row.get(k, v) for k, v in _P_FIELDS.items()}
    r["last_seen"], r["created_at"] = tm.iso(r["last_seen"]), tm.iso(r["created_at"])
    await DocTable(PUPPETS).aput(r["actor_uri"], r)


# ============================================================================================ ledger

def ledger_key(platform: str, instance_url: str, note_id: str) -> str:
    raw = "\x1f".join([platform or "", instance_url or "", (note_id or "")[:255]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _by_uri(rows, uri):
    hits = [r for _k, r in rows if r.get("note_uri") == uri]
    r = min(hits, key=lambda r: r.get("created_at") or "") if hits else None
    return _lrow(r) if r is not None else None


def _ledger_rows(col: str, values) -> list:
    """[(key, row)] to scan for `col` in `values`: the table in memory."""
    return tm.view(LEDGER)


async def _aledger_rows(col: str, values) -> list:
    return await tm.aview(LEDGER)


def delivered_by_uri(uri: str) -> Row | None:
    return _by_uri(_ledger_rows("note_uri", [uri]), uri) if uri else None


async def adelivered_by_uri(uri: str) -> Row | None:
    return _by_uri(await _aledger_rows("note_uri", [uri]), uri) if uri else None


def _by_event(rows, eid):
    hits = [r for _k, r in rows if r.get("nostr_event_id") == eid]
    r = min(hits, key=lambda r: r.get("created_at") or "") if hits else None
    return _lrow(r) if r is not None else None


def delivered_by_event(eid: str) -> Row | None:
    return _by_event(_ledger_rows("nostr_event_id", [eid]), eid) if eid else None


async def adelivered_by_event(eid: str) -> Row | None:
    return _by_event(await _aledger_rows("nostr_event_id", [eid]), eid) if eid else None


async def adelivered(platform: str, instance_url: str, note_id: str) -> bool:
    t = DocTable(LEDGER)
    return await t.aget(ledger_key(platform, instance_url, note_id)) is not None


def mirrored_among(event_ids) -> set:
    """Which of these event ids are notes mirrored from the fediverse (a ledger row WITH a note URI)."""
    ids = set(event_ids)
    return {r.get("nostr_event_id") for _k, r in _ledger_rows("nostr_event_id", ids)
            if r.get("nostr_event_id") in ids and r.get("note_uri") is not None}


async def amirrored_among(event_ids) -> set:
    ids = set(event_ids)
    return {r.get("nostr_event_id") for _k, r in await _aledger_rows("nostr_event_id", ids)
            if r.get("nostr_event_id") in ids and r.get("note_uri") is not None}


async def arecord(*, platform: str, instance_url: str, note_id: str, nostr_event_id: str,
                  note_uri: str | None = None, author_acct: str | None = None,
                  nostr_pubkey: str | None = None) -> None:
    """Record a handled note/DM. Raises Unavailable when it was not stored."""
    row = {"platform": platform, "instance_url": instance_url or "", "note_id": (note_id or "")[:255],
           "note_uri": note_uri[:512] if note_uri else None, "author_acct": author_acct or None,
           "nostr_event_id": nostr_event_id or "", "nostr_pubkey": nostr_pubkey or None,
           "created_at": tm.iso(datetime.utcnow()), "deleted_at": None}
    await DocTable(LEDGER).aput(ledger_key(platform, instance_url, note_id), row)


def record(**kw) -> None:
    """Sync `arecord` (worker threads)."""
    from app.services.doc_table import _run
    _run(arecord(**kw))


def forget_uri(uri: str) -> int:
    from app.services.doc_table import _run
    return _run(aforget_uri(uri))


async def aforget_uri(uri: str) -> int:
    """Drop every ledger row for a note URI (its event was deleted). Raises Unavailable."""
    n = 0
    for k, r in (await _aledger_rows("note_uri", [uri]) if uri else []):
        if r.get("note_uri") == uri:
            await DocTable(LEDGER).adelete(k)
            n += 1
    return n


async def aprune(keep_days: int) -> int:
    """Delete rows older than `keep_days` (0 keeps everything). Raises Unavailable."""
    if keep_days <= 0:
        return 0
    cutoff = tm.iso(datetime.utcnow() - timedelta(days=keep_days))
    n = 0
    for k, r in await tm.aview(LEDGER):
        if (r.get("created_at") or "") < cutoff:
            await DocTable(LEDGER).adelete(k)
            n += 1
    return n


async def aready() -> None:
    """Both tables readable in WHOLE -- raises Unavailable otherwise (Loading while a restart's load runs).
    An entry point that will make decisions from them (the inbox, the DM listener) asks this FIRST and
    refuses or waits, so that the lookups below it cannot fail half-way through a delivery."""
    for name in (PUPPETS, LEDGER):
        await DocTable(name)._ensure()
