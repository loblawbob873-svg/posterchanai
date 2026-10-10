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

Until each table's migration marker exists SQL is still its store of record (#161 wave 1): DocTable answers
it through the Legacies at the bottom of this module and writes SQL first, then the relay. After that, lookups
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
    """[(key, row)] to scan for `col` in `values`: before the marker SQL's matching rows (by its own index --
    never the whole ledger per lookup), after it the table in memory."""
    if not DocTable(LEDGER).migrated():
        from app.services import doc_table
        return doc_table._sql(lambda db: _LEDGER_LEGACY.where_in(db, col, values))
    return tm.view(LEDGER)


async def _aledger_rows(col: str, values) -> list:
    if not await DocTable(LEDGER).amigrated():
        import asyncio
        from app.services import doc_table
        return await asyncio.to_thread(doc_table._sql, lambda db: _LEDGER_LEGACY.where_in(db, col, values))
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
    if not await t.amigrated():             # SQL, by its own index -- never a scan of the whole ledger
        import asyncio
        from app.services import doc_table
        return await asyncio.to_thread(doc_table._sql, lambda db: _LEDGER_LEGACY.has_note(
            db, platform, instance_url, note_id))
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


# ============================================================================================ the SQL side
# (#161 wave 1) Until a table's marker exists SQL is its store of record; these are how it is read and written.

def _puppet_from_sql(p) -> dict:
    return {"actor_uri": p.actor_uri, "acct": p.acct, "instance_host": p.instance_host,
            "pubkey_hex": p.pubkey_hex, "nip05_name": p.nip05_name,
            "display_name": p.display_name, "avatar_url": p.avatar_url,
            "profile_sig": p.profile_sig, "last_seen": tm.iso(p.last_seen),
            "created_at": tm.iso(p.created_at)}


def puppets_from_sql(db) -> dict:
    from app.models import FediPuppet
    return {p.actor_uri: _puppet_from_sql(p) for p in db.query(FediPuppet).all()}


def _ledger_row_from_sql(r) -> dict:
    return {"platform": r.platform, "instance_url": r.instance_url or "", "note_id": (r.note_id or "")[:255],
            "note_uri": r.note_uri, "author_acct": r.author_acct, "nostr_event_id": r.nostr_event_id or "",
            "nostr_pubkey": r.nostr_pubkey, "created_at": tm.iso(r.created_at),
            "deleted_at": tm.iso(r.deleted_at)}


def ledger_from_sql(db, keep_days: int) -> dict:
    """The ledger rows the prune would keep (rows past retention are about events the relay no longer
    holds and would be deleted by the next daily prune). Rows sharing a (platform, instance, note) collapse
    into the FIRST one recorded, which is the one dedup ever consulted."""
    from app.models import FediBridgeDelivered as F
    q = db.query(F)
    if keep_days > 0:
        q = q.filter(F.created_at >= datetime.utcnow() - timedelta(days=keep_days))
    out = {}
    for r in q.order_by(F.id.asc()).all():
        k = ledger_key(r.platform, r.instance_url, r.note_id)
        if k in out:
            continue
        out[k] = _ledger_row_from_sql(r)
    return out


def _keep_days() -> int:
    from app.services import settings_store
    try:
        return int(settings_store.get("nostr_relay_retention_days", "30") or "30")
    except ValueError:
        return 30


from app.services.doc_table import Legacy as _Legacy  # noqa: E402


class LedgerLegacy(_Legacy):
    """`fedi_bridge_delivered`, keyed by sha256(platform, instance_url, note_id) -- a key SQL cannot look up, so a
    point read by key finds the row through those three columns, which every document carries."""
    name = LEDGER

    def rows(self, db) -> dict:
        return ledger_from_sql(db, _keep_days())

    @staticmethod
    def _matching(db, platform, instance_url, note_id):
        from app.models import FediBridgeDelivered as F
        return db.query(F).filter(F.platform == platform, F.instance_url == (instance_url or ""),
                                  F.note_id == (note_id or "")[:255]).order_by(F.id.asc())

    def where_in(self, db, col: str, values) -> list:
        """[(key, row)] of the rows whose `col` is one of `values` -- the same rows `rows()` holds for them."""
        from app.models import FediBridgeDelivered as F
        values = [v for v in values if v]
        if not values:
            return []
        q = db.query(F).filter(getattr(F, col).in_(values))
        keep = _keep_days()
        if keep > 0:
            q = q.filter(F.created_at >= datetime.utcnow() - timedelta(days=keep))
        out = {}
        for r in q.order_by(F.id.asc()).all():
            out.setdefault(ledger_key(r.platform, r.instance_url, r.note_id), _ledger_row_from_sql(r))
        return list(out.items())

    def has_note(self, db, platform, instance_url, note_id) -> bool:
        return self._matching(db, platform, instance_url, note_id).first() is not None

    def _by_key(self, db, k):
        # the key is a hash: find the row by reading only the three columns it is made of
        from app.models import FediBridgeDelivered as F
        for rid, p, i, n in db.query(F.id, F.platform, F.instance_url, F.note_id).order_by(F.id.asc()):
            if ledger_key(p, i, n) == k:
                return db.get(F, rid)
        return None

    def get(self, db, k):
        r = self._by_key(db, k)
        if r is None:
            return None
        keep = _keep_days()
        if keep > 0 and r.created_at is not None and r.created_at < datetime.utcnow() - timedelta(days=keep):
            return None
        return _ledger_row_from_sql(r)

    def put(self, db, k, row) -> None:
        from app.models import FediBridgeDelivered as F
        from app.services.legacy_sql import to_datetime
        r = self._matching(db, row.get("platform"), row.get("instance_url"), row.get("note_id")).first()
        if r is None:
            r = F()
            db.add(r)
        r.platform = row.get("platform") or ""
        r.instance_url = row.get("instance_url") or ""
        r.note_id = (row.get("note_id") or "")[:255]
        r.note_uri = row.get("note_uri")
        r.author_acct = row.get("author_acct")
        r.nostr_event_id = row.get("nostr_event_id") or ""
        r.nostr_pubkey = row.get("nostr_pubkey")
        r.created_at = to_datetime(row.get("created_at"))
        r.deleted_at = to_datetime(row.get("deleted_at"))
        db.flush()

    def delete(self, db, k) -> None:
        first = self._by_key(db, k)
        if first is not None:
            for r in self._matching(db, first.platform, first.instance_url, first.note_id).all():
                db.delete(r)
            db.flush()


_LEDGER_LEGACY = LedgerLegacy()


def _legacies_from_sql():
    from app.services.legacy_sql import ModelLegacy
    return (ModelLegacy(PUPPETS, "FediPuppet", _puppet_from_sql, key_of=lambda p: p.actor_uri, int_ids=False),
            _LEDGER_LEGACY)


def _register_legacy():
    from app.services import table_migration
    for lg in _legacies_from_sql():
        table_migration.register(lg)


_register_legacy()




async def aready() -> None:
    """Both tables readable in WHOLE -- raises Unavailable otherwise (Loading while a restart's load runs).
    An entry point that will make decisions from them (the inbox, the DM listener) asks this FIRST and
    refuses or waits, so that the lookups below it cannot fail half-way through a delivery. Before a table's
    marker SQL answers it, so there is nothing to wait for."""
    for name in (PUPPETS, LEDGER):
        t = DocTable(name)
        if await t.amigrated():
            await t._ensure()
