"""One-time copy of `blossom_blobs` + `blossom_blob_owners` (SQL) into the blob index documents (#161).

Read-only on SQL: nothing is dropped, no row is deleted. Both tables are read whole -- the copy is only ever
verified against the COMPLETE SQL table -- and merged into one document per blob (see blob_index). Run by
table_migration (port-3051 startup, a background thread) and `scripts/migrate_tables_to_nostr.py --table
blossom_blobs`; `sql_rows` is also `BlobLegacy.rows`, what the app reads before the marker exists.
"""
import asyncio
import logging
import time

from app.services import blob_index
from app.services.doc_table_bulk import amigrate_rows

logger = logging.getLogger(__name__)


def sql_rows(db) -> tuple[dict, dict]:
    """({sha: row}, stats) from the two SQL tables -- the whole of both, in bounded batches."""
    from app.models import BlossomBlob, BlossomBlobOwner
    out: dict = {}
    for b in db.query(BlossomBlob).order_by(BlossomBlob.sha256).yield_per(5000):
        out[b.sha256] = blob_index.new_row(
            pubkey=b.pubkey, size=b.size, mime=b.mime, created_at=b.created_at, expires_at=b.expires_at,
            storage=b.storage, path=b.path, private=bool(b.private), keep=bool(b.keep))
    owners = dangling = 0
    for o in db.query(BlossomBlobOwner).order_by(BlossomBlobOwner.sha256).yield_per(5000):
        row = out.get(o.sha256)
        if row is None:
            dangling += 1           # an owner of a blob that has no row: the FK cascade makes this 0
            continue
        row["owners"][o.pubkey] = [int(o.created_at or 0), o.name or None]
        owners += 1
    return out, {"owners": owners, "dangling_owner_rows": dangling}


async def amigrate(db) -> dict:
    """Copy through the one engine (doc_table_bulk.amigrate_rows) against THIS session: the verification
    re-reads it fresh, because SQL stays the store of record (and is written to) until the marker exists."""
    t0 = time.time()
    rows, stats = await asyncio.to_thread(sql_rows, db)
    stats["sql_read_s"] = round(time.time() - t0, 1)
    return await amigrate_rows(blob_index.TABLE, lambda: sql_rows(db)[0], extra=stats,
                               point=lambda k: blob_index._LEGACY.get(db, k))


def migrate() -> dict:
    """Synchronous entry (a thread of its own, or the script). Idempotent; raises on mismatch."""
    from app.services import table_migration
    return table_migration.migrate(blob_index.TABLE)
