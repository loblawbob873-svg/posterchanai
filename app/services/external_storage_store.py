"""File-manager external storage mounts as a DocTable (#161) -- what used to be the SQL `external_storage`
table plus its `external_storage_users` junction.

One document per mount, keyed by its integer id (the id Admin → Storage already uses, kept across the
copy). The allowed users are carried IN the row as `allowed_user_ids` (the junction table's rows);
access is still "the user's id is in that list" -- an empty list grants nobody, as before. A mount's
real path is server configuration and stays out of logs. A table that cannot be read raises
`relay_reader.Unavailable`: a path is then neither treated as a mount nor as the user's own folder
(the caller answers 503).
"""
from __future__ import annotations

from datetime import datetime

from app.services import table_migrate as tm
from app.services.doc_table import DocTable
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
        kid = DocTable(TABLE).insert(r, lambda: tm.anext_id(TABLE))
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
