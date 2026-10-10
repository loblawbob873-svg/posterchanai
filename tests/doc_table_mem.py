"""An in-memory stand-in for the relay BEHIND DocTable, for unit tests of code that uses app tables (#161).

The tables' own rules -- strict loads, relay-first writes, the live stream, NIP-09 deletions -- are pinned
against the real RelayServer + PosterChanDB in tests/test_doc_table.py and tests/test_tables_fedi_auth.py.
Here only the transport is replaced (DocTable.aload/aput/adelete and the migration re-read), so the code
under test runs its real table lookups. `mem.down = True` makes every read AND write "could not ask"
(relay_reader.Unavailable) -- the failure the callers must refuse on; `mem.writes_fail = True` only the
writes.

    from tests.doc_table_mem import mem_tables
    def test_x(monkeypatch):
        mem = mem_tables(monkeypatch)                 # every table migrated and empty
        mem.put("api_keys", "1", {...})               # seed a row as the relay would hold it
"""
import json
import time

from app.services import doc_table, table_migrate
from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

TABLES = ("fedi_puppets", "fedi_bridge_delivered", "external_storage", "api_keys", "shared_files",
          "verification_tokens")


class MemRelay:
    def __init__(self):
        self.rows: dict = {}            # (table, key) -> row
        self.down = False
        self.writes_fail = False
        self.writes = 0

    def table(self, name) -> dict:
        return {k: json.loads(json.dumps(v)) for (t, k), v in self.rows.items() if t == name}

    def put(self, name, key, row):
        self.rows[(name, str(key))] = json.loads(json.dumps(row, default=str))
        t = DocTable._registry.get(name)
        if t is not None and t._loaded_at:
            with t._lock:
                t._rows[str(key)] = json.loads(json.dumps(row, default=str))


def mem_tables(monkeypatch, *, migrated=TABLES) -> MemRelay:
    mem = MemRelay()
    for name in migrated:
        mem.rows[(table_migrate.MARKERS, name)] = {"ok": True, "verified": True, "rows": 0}

    async def aload(self, *, force=False):
        if self._loaded_at and not force:
            return
        if mem.down:
            raise Unavailable("relay down (test)")
        with self._lock:
            self._rows = mem.table(self.name)
            self._stamp = {k: 0 for k in self._rows}
            self._loaded_at = time.time()

    async def aput(self, k, row):
        if mem.down or mem.writes_fail:
            raise Unavailable("relay did not store (test)")
        payload = json.loads(json.dumps(row, default=str))
        mem.rows[(self.name, str(k))] = payload
        mem.writes += 1
        with self._lock:
            self._rows[str(k)] = json.loads(json.dumps(payload))

    async def adelete(self, k):
        if mem.down or mem.writes_fail:
            raise Unavailable("relay did not delete (test)")
        mem.rows.pop((self.name, str(k)), None)
        mem.writes += 1
        with self._lock:
            self._rows.pop(str(k), None)

    async def reread(name):
        if mem.down:
            raise Unavailable("relay down (test)")
        return mem.table(name)

    async def aget_remote(self, k):
        if mem.down:
            raise Unavailable("relay down (test)")
        row = mem.rows.get((self.name, str(k)))
        return json.loads(json.dumps(row)) if row is not None else None

    async def bulk_put(table, rows, **_kw):
        if mem.down or mem.writes_fail:
            raise Unavailable("relay did not store (test)")
        for k, row in rows.items():
            mem.rows[(table, str(k))] = json.loads(json.dumps(row, default=str))
        return len(rows)

    async def delete_doc(port, sk, d_tag, **_kw):
        if mem.down or mem.writes_fail:
            raise Unavailable("relay did not delete (test)")
        for (t, k) in list(mem.rows):
            if d_tag == DocTable(t).prefix + doc_table.key(k):
                mem.rows.pop((t, k), None)
        return True

    monkeypatch.setattr(DocTable, "aload", aload)
    monkeypatch.setattr(DocTable, "aput", aput)
    monkeypatch.setattr(DocTable, "_relay_put", aput)
    monkeypatch.setattr(DocTable, "adelete", adelete)
    monkeypatch.setattr(DocTable, "_relay_delete", adelete)
    monkeypatch.setattr(DocTable, "aget_remote", aget_remote)
    monkeypatch.setattr(DocTable, "_start_live", lambda self, pk: None)
    monkeypatch.setattr(doc_table, "_operator", lambda: (b"\x01" * 32, "00" * 32))
    from app.services import doc_table_bulk
    monkeypatch.setattr(doc_table_bulk, "aread_all", reread)
    monkeypatch.setattr(doc_table_bulk, "abulk_put", bulk_put)
    monkeypatch.setattr(doc_table_bulk.nostr_store, "delete_doc", delete_doc)
    monkeypatch.setattr(DocTable, "_registry", {})
    # the markers this "process" has seen: none yet -- it reads them from `mem` like a fresh process would
    monkeypatch.setattr(doc_table, "_marked", set())
    monkeypatch.setattr(doc_table, "_neg", {})
    return mem



# ------------------------------------------------------------------------------------ the old test idiom
# Tests written against the SQL bridge tables seed and inspect them through a session:
#     s = Session(); s.add(FediPuppet(...)); s.commit(); s.query(FediPuppet).filter(FediPuppet.x == y).one()
# FakeSession keeps that idiom working over the DocTables, so the assertions stay word for word: `add` turns
# the ORM object into the row the table stores, `query` reads the table back (== / != / like / in_ only).

def _orm_to_row(obj):
    from datetime import datetime
    from app.models import FediBridgeDelivered, FediPuppet
    from app.services import fedi_tables
    from app.services.table_migrate import iso
    if isinstance(obj, FediPuppet):
        row = {k: getattr(obj, k) for k in fedi_tables._P_FIELDS}
        row["last_seen"] = iso(row["last_seen"] or datetime.utcnow())
        row["created_at"] = iso(row["created_at"] or datetime.utcnow())
        return fedi_tables.PUPPETS, row["actor_uri"], row
    if isinstance(obj, FediBridgeDelivered):
        row = {k: getattr(obj, k) for k in fedi_tables._L_FIELDS}
        row["created_at"] = iso(row["created_at"] or datetime.utcnow())
        row["deleted_at"] = iso(row["deleted_at"])
        row["instance_url"] = row["instance_url"] or ""
        return fedi_tables.LEDGER, fedi_tables.ledger_key(row["platform"], row["instance_url"], row["note_id"]), row
    raise TypeError("FakeSession only holds the DocTable-backed models, not %r" % type(obj))


def _table_of(model):
    from app.models import FediBridgeDelivered, FediPuppet
    from app.services import fedi_tables
    return {FediPuppet: (fedi_tables.PUPPETS, fedi_tables._P_FIELDS),
            FediBridgeDelivered: (fedi_tables.LEDGER, fedi_tables._L_FIELDS)}[model]


def _holds(cond, row) -> bool:
    import re
    from sqlalchemy.sql import operators
    col = cond.left.key
    op = cond.operator
    right = cond.right
    val = getattr(right, "value", None)
    have = row.get(col)
    if op is operators.eq:
        return have == val
    if op is operators.ne:
        return have != val
    if op is operators.is_not:
        return have is not None
    if op is operators.is_:
        return have is None
    if op is operators.like_op:
        pat = "^" + ".*".join(re.escape(p) for p in str(val).split("%")) + "$"
        return have is not None and re.match(pat, str(have)) is not None
    if op is operators.in_op:
        return have in (val or [])
    raise NotImplementedError(op)


class _Query:
    def __init__(self, mem, model, conds=()):
        self.mem, self.model, self.conds = mem, model, list(conds)

    def filter(self, *conds):
        return _Query(self.mem, self.model, self.conds + list(conds))

    def _rows(self):
        from app.services.table_migrate import Row
        name, fields = _table_of(self.model)
        out = []
        for k, r in sorted(self.mem.table(name).items()):
            row = {**fields, **r}
            if all(_holds(c, row) for c in self.conds):
                out.append((k, Row(row)))
        return out

    def all(self):
        return [r for _k, r in self._rows()]

    def __iter__(self):
        return iter(self.all())

    def first(self):
        rows = self.all()
        return rows[0] if rows else None

    def one(self):
        rows = self.all()
        assert len(rows) == 1, "expected exactly one row, got %d" % len(rows)
        return rows[0]

    def count(self):
        return len(self._rows())

    def delete(self, synchronize_session=None):
        name, _ = _table_of(self.model)
        rows = self._rows()
        for k, _r in rows:
            self.mem.rows.pop((name, k), None)
            t = DocTable._registry.get(name)
            if t is not None:
                with t._lock:
                    t._rows.pop(k, None)
        return len(rows)


class FakeSession:
    def __init__(self, mem):
        self.mem = mem

    def add(self, obj):
        name, key, row = _orm_to_row(obj)
        self.mem.put(name, key, row)

    def query(self, model):
        return _Query(self.mem, model)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass
