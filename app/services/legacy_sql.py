"""The SQL side of a table that is moving to a DocTable (#161 wave 1) -- the generic part.

`doc_table.Legacy` is the contract: until a table's `_migrated` marker exists, DocTable answers its reads from
SQL through it and writes SQL first, the relay second. Most of the moved tables are one SQLAlchemy model whose
row dict uses the model's own column names, so `ModelLegacy` does them all from three facts: the model, how a
model object becomes the row dict (`to_row` -- the SAME function the migration copies with, so a row read from
SQL before the marker and one read from the relay after it are identical) and how a key finds the object.

Tables that are not one model per row (the Blossom index: blob + owners; the follow-seen shards; the Telegram
reply map keyed by chat and message) subclass `Legacy` themselves, next to their store.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime

from app.services.doc_table import Legacy


def to_datetime(value):
    """A stored value (naive-UTC ISO string, unix seconds, or a datetime) as the naive-UTC datetime SQL holds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        d = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    else:
        try:
            d = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


class ModelLegacy(Legacy):
    """One SQLAlchemy model, one row per object.

    name      -- the DocTable's name
    model     -- the SQLAlchemy model class, or its NAME in app.models (resolved on use)
    to_row    -- obj -> row dict (exactly what the relay copy holds)
    key_of    -- obj -> key (default: str(obj.id))
    int_ids   -- the key is the model's integer `id`: a new row takes SQL's id before the marker
    to_cols   -- row dict -> {column: value} to assign (default: every column of the model the row names, with
                 DateTime columns parsed back from their stored form)
    find      -- (db, key) -> obj or None (default: the primary key)
    """

    def __init__(self, name, model, to_row, *, key_of=None, int_ids=True, to_cols=None, find=None,
                 skip_row=None):
        self.name = name
        self._model = model
        self.to_row = to_row
        self.key_of = key_of or (lambda o: str(o.id))
        self.int_ids = int_ids
        self._to_cols = to_cols
        self._find = find
        self._skip = skip_row or (lambda o: False)

    @property
    def model(self):
        """The model class, looked up in app.models when it is used (a name is resolved every time: a module
        re-imported in isolation must not leave a stale class behind in a registry that outlives it)."""
        if isinstance(self._model, str):
            import sys
            return getattr(sys.modules.get("app.models") or __import__("app.models", fromlist=["x"]), self._model)
        return self._model

    # -- reading
    def rows(self, db) -> dict:
        out = {}
        for o in db.query(self.model).all():
            if not self._skip(o):
                out[self.key_of(o)] = self.to_row(o)
        return out

    def find(self, db, k):
        if self._find is not None:
            return self._find(db, k)
        if self.int_ids:
            try:
                return db.get(self.model, int(k))
            except (TypeError, ValueError):
                return None
        return db.get(self.model, k)

    def get(self, db, k):
        o = self.find(db, k)
        return None if o is None or self._skip(o) else self.to_row(o)

    # -- writing
    def cols(self, row: dict) -> dict:
        if self._to_cols is not None:
            return self._to_cols(row)
        out = {}
        for c in self.model.__table__.columns:
            if c.name in row:
                v = row[c.name]
                out[c.name] = to_datetime(v) if isinstance(c.type, DateTime) else v
        return out

    def _assign(self, o, row: dict, *, keep_pk: bool) -> None:
        pks = {c.name for c in self.model.__table__.primary_key.columns}
        for name, v in self.cols(row).items():
            if keep_pk and name in pks:
                continue
            setattr(o, name, v)

    def put(self, db, k, row: dict) -> None:
        o = self.find(db, k)
        if o is None:
            o = self.model()
            if self.int_ids and str(k).isdigit():
                o.id = int(k)
            self._assign(o, row, keep_pk=self.int_ids)
            db.add(o)
        else:
            self._assign(o, row, keep_pk=True)
        db.flush()

    def delete(self, db, k) -> None:
        o = self.find(db, k)
        if o is not None:
            db.delete(o)
            db.flush()

    def insert(self, db, row: dict):
        o = self.model()
        self._assign(o, row, keep_pk=True)
        db.add(o)
        db.flush()
        if "id" in row:
            row["id"] = int(o.id)
        return self.key_of(o)
