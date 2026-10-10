"""The SQL -> DocTable move for one app table (#161), and the small row helpers its stores share.

A table converted to DocTable is answered FROM SQL until its rows were copied and verified (#161 wave 1: its
`doc_table.Legacy`; writes go to SQL first, then the relay) -- an unmigrated relay copy is never read as the
table, because empty is the most dangerous answer these tables can give: an API key that "does not exist" is a
refused user, a puppet that "does not exist" gets a second identity minted beside the real one, a delivered-note
ledger that "is empty" delivers everything twice.

Migration runs through the one engine (doc_table_bulk.amigrate_rows, registry in table_migration): copy, re-read
strictly, compare with SQL, settle, marker. The SQL rows are never dropped or changed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from app.services.doc_table import DocTable
from app.services.doc_table_bulk import MigrationMismatch  # noqa: F401 -- the name this batch's callers catch

logger = logging.getLogger(__name__)

MARKERS = "_migrated"
SEQ = "_seq"


class Row(dict):
    """A table row (a plain dict, which is what is stored) that also reads like the ORM row it replaced."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k, v):
        self[k] = v


def iso(dt) -> str | None:
    """A datetime as it is stored (naive UTC ISO-8601, which is also what FastAPI used to answer)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if d.tzinfo is not None:            # compared with naive utcnow() everywhere: normalise to naive UTC
        from datetime import timezone
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def canon(row: dict) -> dict:
    """Exactly what DocTable stores for `row` (its JSON round trip)."""
    return json.loads(json.dumps(row, default=str))


# ------------------------------------------------------------------------------------ reads
# There is no readiness gate any more (#161 wave 1): until a table's marker exists DocTable answers it from SQL
# (its Legacy) and writes SQL then the relay, so "not migrated yet" is never "could not ask". These helpers keep
# the names this batch's stores were written against.

def require(name: str) -> None:
    return None


async def arequire(name: str) -> None:
    return None


def view(name: str) -> list:
    """[(key, row)] of the WHOLE table without copying the rows (callers read, never mutate them): SQL's rows
    before the marker, memory after. Sync code off the loop loads the table if it must; on an event loop -- or
    while the background load runs -- an unloaded table is Unavailable (Loading), never a blocked loop."""
    return DocTable(name).view()


def row(name: str, k) -> dict | None:
    """ONE row by key (a copy): SQL before the marker, memory once loaded, the relay's one document while the
    table loads (off the loop)."""
    return DocTable(name).get(k)


async def aview(name: str) -> list:
    return await DocTable(name).aview()


async def arow(name: str, k) -> dict | None:
    return await DocTable(name).aget(k)


# ------------------------------------------------------------------------------------ ids
def next_id(name: str) -> int:
    """A fresh integer id once the relay is authoritative (table_migration.next_id); before the marker a new
    row takes SQL's id through `DocTable.insert`."""
    from app.services import table_migration
    return table_migration.next_id(name)


async def anext_id(name: str) -> int:
    from app.services import table_migration
    return await table_migration.anext_id(name)


from app.services import doc_table as _doc_table  # noqa: E402
_ready = _doc_table._marked      # the markers this process has seen
