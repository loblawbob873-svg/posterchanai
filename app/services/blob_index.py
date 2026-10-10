"""The Blossom blob index -- `blossom_blobs` + `blossom_blob_owners` -- as Nostr documents (#161).

ONE DOCUMENT PER BLOB, its owners inside it. `DocTable("blossom_blobs")` row `<sha256>`:

    {"pubkey", "size", "mime", "created_at", "expires_at", "storage", "path", "private", "keep",
     "owners": {"<hex pubkey>": [added_at, "original filename" | null], ...}}

The two SQL tables were one fact split across a foreign key (`ON DELETE CASCADE`): an owner row never outlived
its blob, and every list, delete and quota query joined them. Kept as two document tables they would be two
writes that can half-land and twice the documents to load. Embedded, a blob and who references it change in
one write, and deleting the blob takes its owners with it -- the cascade, by construction.

WHAT EACH PATH MAY READ, and why it differs (measured, 100k synthetic rows -- see the report in the commit):

  * a WHOLE-TABLE decision -- a BUD-02 listing, quota, the age sweep, the store scan, forget-missing, a purge --
    reads the table held in memory, which is loaded STRICTLY (every document or an exception). Until that load
    has succeeded those paths raise `Unavailable`; none of them ever runs on a partial table. Above all the
    sweep and the repair, which delete: "could not ask" is never "no rows";
  * a POINT lookup (GET a blob, an upload's dedup check, an owner check) needs one row. While the big table is
    still loading -- about a minute on a large node, in a background thread -- it reads that one document from
    the relay instead (strict: None only when the relay answered and holds nothing), so serving files does not
    wait on the load;
  * a WRITE reads the row it changes (memory or relay), applies the change, writes the whole row back -- under
    a per-blob lock, so two uploads of the same bytes in this process do not lose an owner. This process is the
    only writer (the app on port 3051); other processes' writes still arrive via DocTable's live stream.

RELAY-ONLY from the first request (no SQL side). A node's old `blossom_blobs` + `blossom_blob_owners` rows are
merged into these documents once by scripts/migrate_tables_to_nostr.py.
"""
import asyncio
import logging
import threading
from collections import namedtuple

from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

TABLE = "blossom_blobs"
FIELDS = ("pubkey", "size", "mime", "created_at", "expires_at", "storage", "path", "private", "keep")

# Attribute-compatible with the ORM row it replaces (blob.sha256, blob.size, blob.keep ...), immutable.
Blob = namedtuple("Blob", ("sha256",) + FIELDS + ("owners",))
_RESYNC_S = 6 * 3600       # backstop full reload; the live stream (with gap replay) keeps memory current between


class NotMigrated(Unavailable):
    """Kept for callers that catch it; nothing raises it (there is no SQL side)."""


def table() -> DocTable:
    t = DocTable(TABLE)
    if not getattr(t, "_blob_index_watch", False):
        t._blob_index_watch = True
        t.resync_s = _RESYNC_S
        t.watch(_on_change)
        if t.loaded:                    # loaded before anything watched it: build the owner index now
            _on_change(None, None)
    return t


def blob_from_row(sha: str, row: dict | None):
    if row is None:
        return None
    return Blob(sha, row.get("pubkey") or "", int(row.get("size") or 0), row.get("mime"),
                int(row.get("created_at") or 0), row.get("expires_at"), row.get("storage") or "local",
                row.get("path") or "", bool(row.get("private")), bool(row.get("keep")),
                dict(row.get("owners") or {}))


# ------------------------------------------------------------------ the store of record
async def amigrated() -> bool:
    """Always True: the relay is the only store (kept for callers that still ask)."""
    return True


def migrated() -> bool:
    return True


def _require_loaded() -> DocTable:
    """The table, for a whole-table read once the relay is the store of record: Loading (an Unavailable with a
    Retry-After) while the background load runs -- never an answer from part of the table."""
    t = table()
    if not t.loaded:
        t.rows_view()           # raises Loading while the loader runs, Unavailable otherwise
    return t


# ------------------------------------------------------------------ owner index (secondary, in memory)
_idx_lock = threading.Lock()
_by_owner: dict = {}            # pubkey -> set(sha)
_owners_of: dict = {}           # sha -> frozenset(pubkey), so a change can be undone from _by_owner


def _index_row(sha, row):
    old = _owners_of.pop(sha, ())
    for pk in old:
        s = _by_owner.get(pk)
        if s is not None:
            s.discard(sha)
            if not s:
                _by_owner.pop(pk, None)
    if row is not None:
        pks = frozenset((row.get("owners") or {}).keys())
        if pks:
            _owners_of[sha] = pks
            for pk in pks:
                _by_owner.setdefault(pk, set()).add(sha)


def _on_change(k, row):
    with _idx_lock:
        if k is None:
            _by_owner.clear()
            _owners_of.clear()
            for sha, r in DocTable(TABLE).rows_view():
                _index_row(sha, r)
        else:
            _index_row(k, row)


# ------------------------------------------------------------------ reads
async def _aread(t: DocTable, sha: str):
    """One row: memory once loaded, else that one document from the relay (strict)."""
    if t.loaded:
        cur = t.peek(sha)
        return None if cur is None else _copy(cur)
    return await t.aget_remote(sha)


async def aget(sha: str):
    """The Blob for `sha`, or None when the index answered and holds no such blob. Raises Unavailable when it
    could not be asked."""
    return blob_from_row(sha, await _aread(table(), sha))


def get(sha: str):
    """Synchronous point read -- memory only (Unavailable until loaded)."""
    return blob_from_row(sha, _require_loaded().peek(sha))


def rows() -> list:
    """[Blob] for EVERY blob -- the complete table or Unavailable, never part of it."""
    return [blob_from_row(k, r) for k, r in _require_loaded().rows_view()]


def count() -> int:
    return len(_require_loaded().rows_view())


def owned_by(pubkey_hex: str) -> list:
    """[Blob] this pubkey holds a reference to (complete, or Unavailable)."""
    t = _require_loaded()
    with _idx_lock:
        shas = list(_by_owner.get(pubkey_hex, ()))
    out = []
    for sha in shas:
        b = blob_from_row(sha, t.peek(sha))
        if b is not None:
            out.append(b)
    return out


# ------------------------------------------------------------------ writes
_locks: dict = {}               # (id(loop), slot) -> asyncio.Lock; per loop, because a lock binds to its loop
_N_SLOTS = 64


def _lock_for(sha: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), hash(sha) % _N_SLOTS)
    lk = _locks.get(key)
    if lk is None:
        for k in [k for k in _locks if k[0] != id(loop) and len(_locks) > 4 * _N_SLOTS]:
            _locks.pop(k, None)
        lk = _locks[key] = asyncio.Lock()
    return lk


async def aupdate(sha: str, fn):
    """Read-modify-write ONE blob. `fn(row_copy_or_None)` returns the new row (written), or None (nothing to
    write). Returns the Blob as it stands afterwards. Raises Unavailable if the row cannot be read or written --
    a write that did not land is never reported as done."""
    t = table()
    async with _lock_for(sha):
        cur = await _aread(t, sha)
        new = fn(None if cur is None else _copy(cur))
        if new is None:
            return blob_from_row(sha, cur)
        await t.aput(sha, new)
        return blob_from_row(sha, new)


async def adelete(sha: str) -> None:
    """Remove a blob's document (and with it every owner -- the old ON DELETE CASCADE). Raises if refused."""
    t = table()
    async with _lock_for(sha):
        await t.adelete(sha)


def _copy(row: dict) -> dict:
    out = dict(row)
    out["owners"] = {k: list(v) for k, v in (row.get("owners") or {}).items()}
    return out


def new_row(*, pubkey, size, mime, created_at, expires_at, storage, path, private=False, keep=False) -> dict:
    return {"pubkey": pubkey, "size": int(size), "mime": mime or None, "created_at": int(created_at),
            "expires_at": expires_at, "storage": storage, "path": path, "private": bool(private),
            "keep": bool(keep), "owners": {}}


# ------------------------------------------------------------------ loading (a background thread, never the loop)
def start_background(migrate=None) -> None:
    """Load the index on a thread of its own (DocTable.start_background_load: it loads and keeps reloading there --
    never on the request loop). `migrate` is accepted for old callers and ignored. Idempotent."""
    table().start_background_load()

