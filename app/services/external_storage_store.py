"""File-manager external storage mounts as a DocTable (#161) -- what used to be the SQL `external_storage`
table plus its `external_storage_users` junction.

One document per mount, keyed by its integer id (the id Admin → Storage already uses, kept across the
migration). The allowed users are carried IN the row as `allowed_user_ids` (the junction table's rows);
access is still "the user's id is in that list" -- an empty list grants nobody, as before. A mount's
real path is server configuration and stays out of logs. A table that cannot be read raises
`relay_reader.Unavailable`: a path is then neither treated as a mount nor as the user's own folder
(the caller answers 503).
"""
from __future__ import annotations

from datetime import datetime

from app.services import table_migrate as tm
from app.services.doc_table import DocTable, Legacy
from app.services.table_migrate import Row

TABLE = "external_storage"
_FIELDS = {"id": 0, "name": "", "mount_path": "", "mount_point": "", "description": None, "is_active": True,
           "created_at": None, "updated_at": None, "allowed_user_ids": []}


def _row(r) -> Row:
    return Row({**_FIELDS, **(r or {})})


def allows(mount, user) -> bool:
    """May `user` use this mount (the old `user in mount.allowed_users`)."""
    return bool(mount) and user is not None and getattr(user, "id", None) in (mount.get("allowed_user_ids") or [])


def _active(rows, mount_point: str):
    hits = [r for _k, r in rows if r.get("mount_point") == mount_point and r.get("is_active") is True]
    return _row(min(hits, key=lambda r: int(r.get("id") or 0))) if hits else None


async def aactive_by_mount_point(mount_point: str) -> Row | None:
    return _active(await tm.aview(TABLE), mount_point)


async def aactive_mounts() -> list:
    rows = [_row(r) for _k, r in await tm.aview(TABLE) if r.get("is_active") is True]
    return sorted(rows, key=lambda r: (r.name or "", int(r.id or 0)))


def all_mounts() -> list:
    return sorted((_row(r) for _k, r in tm.view(TABLE)), key=lambda r: (r.name or "", int(r.id or 0)))


def get(mount_id: int) -> Row | None:
    r = tm.row(TABLE, str(int(mount_id)))
    return _row(r) if r is not None else None


def by_mount_point(mount_point: str, *, exclude_id: int | None = None) -> Row | None:
    for _k, r in tm.view(TABLE):
        if r.get("mount_point") == mount_point and r.get("id") != exclude_id:
            return _row(r)
    return None


def save(row: dict) -> Row:
    """Create (no id) or replace a mount. Raises Unavailable when it was not stored."""
    now = tm.iso(datetime.utcnow())
    r = {**_FIELDS, **row}
    r["updated_at"] = now
    r["allowed_user_ids"] = sorted({int(u) for u in (r.get("allowed_user_ids") or [])})
    if not r.get("id"):
        r["id"] = None
        r["created_at"] = now
        kid = DocTable(TABLE).insert(r, lambda: tm.anext_id(TABLE))     # SQL's id until the marker exists
        r["id"] = int(kid)
        return _row(r)
    DocTable(TABLE).put(str(r["id"]), r)
    return _row(r)


def delete(mount_id: int) -> None:
    DocTable(TABLE).delete(str(int(mount_id)))


def forget_user(user_id: int) -> int:
    """Take a deleted account off every mount's allowed list (the junction rows used to cascade)."""
    n = 0
    for _k, r in tm.view(TABLE):
        ids = r.get("allowed_user_ids") or []
        if user_id in ids:
            save({**r, "allowed_user_ids": [u for u in ids if u != user_id]})
            n += 1
    return n


# ------------------------------------------------------------------------------------ the SQL side (#161 wave 1)
def rows_from_sql(db) -> dict:
    from app.models import ExternalStorage, external_storage_users as J
    allowed: dict = {}
    for sid, uid in db.execute(J.select().with_only_columns(J.c.external_storage_id, J.c.user_id)).fetchall():
        allowed.setdefault(int(sid), set()).add(int(uid))
    return {str(m.id): _mount_from_sql(m, allowed.get(int(m.id), set())) for m in db.query(ExternalStorage).all()}


def _mount_from_sql(m, allowed) -> dict:
    return {"id": int(m.id), "name": m.name, "mount_path": m.mount_path,
            "mount_point": m.mount_point, "description": m.description,
            "is_active": m.is_active is True, "created_at": tm.iso(m.created_at),
            "updated_at": tm.iso(m.updated_at), "allowed_user_ids": sorted(allowed)}


class ExternalStorageLegacy(Legacy):
    """The mount row plus its `external_storage_users` junction rows, as the one document they become."""
    name = TABLE
    int_ids = True

    def rows(self, db) -> dict:
        return rows_from_sql(db)

    def get(self, db, k):
        from app.models import ExternalStorage, external_storage_users as J
        m = db.get(ExternalStorage, int(k))
        if m is None:
            return None
        uids = {int(u) for (u,) in db.execute(J.select().with_only_columns(J.c.user_id)
                                              .where(J.c.external_storage_id == int(k))).fetchall()}
        return _mount_from_sql(m, uids)

    def _write(self, db, m, row) -> None:
        from app.models import external_storage_users as J
        from app.services.legacy_sql import to_datetime
        m.name = row.get("name") or ""
        m.mount_path = row.get("mount_path") or ""
        m.mount_point = row.get("mount_point") or ""
        m.description = row.get("description")
        m.is_active = row.get("is_active") is True
        m.created_at = to_datetime(row.get("created_at"))
        m.updated_at = to_datetime(row.get("updated_at"))
        db.flush()
        db.execute(J.delete().where(J.c.external_storage_id == m.id))
        for uid in sorted({int(u) for u in (row.get("allowed_user_ids") or [])}):
            db.execute(J.insert().values(external_storage_id=m.id, user_id=uid))
        db.flush()

    def put(self, db, k, row) -> None:
        from app.models import ExternalStorage
        m = db.get(ExternalStorage, int(k))
        if m is None:
            m = ExternalStorage(id=int(k))
            db.add(m)
        self._write(db, m, row)

    def insert(self, db, row) -> str:
        from app.models import ExternalStorage
        m = ExternalStorage()
        db.add(m)
        self._write(db, m, row)
        row["id"] = int(m.id)
        return str(m.id)

    def delete(self, db, k) -> None:
        from app.models import ExternalStorage, external_storage_users as J
        db.execute(J.delete().where(J.c.external_storage_id == int(k)))
        db.query(ExternalStorage).filter(ExternalStorage.id == int(k)).delete(synchronize_session=False)
        db.flush()


def _register_legacy():
    from app.services import table_migration
    table_migration.register(ExternalStorageLegacy())


_register_legacy()

