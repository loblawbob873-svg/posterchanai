"""Accounts for tests that used to build SQL `User` rows (#161: accounts are relay DocTable rows -- user_table).

    from tests.doc_table_mem import mem_tables
    from tests.user_fixtures import FakeDB, seed_user

    mem = mem_tables(monkeypatch)
    seed_user(mem, 1, username="alice", nostr_npub=npub, is_admin=True)
    db = FakeDB()
    alice = db.user(1)              # a UserRow this db tracks
    alice.can_ai = False
    db.commit()                     # stores every changed account it handed out (user_table.save)
    db.expire_all()                 # re-reads them in place (what a SQLAlchemy session's expire did)

`FakeDB` is otherwise the app's NoSQL session: any real SQL call raises.
"""
from app.database import NoSQL
from app.services import user_table


def seed_user(mem, uid: int, **fields) -> int:
    """Store account `uid` as the copy script writes a migrated one (its id kept), with its index entries."""
    row = user_table.UserRow(uid, dict({"password_hash": "x"}, **fields)).row()
    mem.put(user_table.TABLE, str(uid), row)
    for k, v in user_table.index_rows({str(uid): row}).items():
        cur = mem.rows.get((user_table.INDEX, k))
        ids = sorted(set((cur or {}).get("ids", [])) | set(v["ids"]))
        mem.put(user_table.INDEX, k, {"field": v["field"], "ids": ids})
    return uid


class FakeDB(NoSQL):
    """The `db` handed around, plus the session-style bookkeeping old tests relied on."""

    def __init__(self):
        object.__setattr__(self, "_handed", [])

    def user(self, uid):
        u = user_table.get(None, uid)
        self._handed.append(u)
        return u

    def commit(self):
        for u in self._handed:
            if u is not None and u.changes():
                user_table.save(None, u)

    def expire_all(self):
        for u in self._handed:
            if u is None:
                continue
            fresh = user_table.get(None, u.id)
            if fresh is not None:
                u._fill(fresh.row())
                u._dirty.clear()


class LiveUsers:
    """`world.users[i]`: account i as the store holds it NOW (each access re-reads it), tracked by `db`."""

    def __init__(self, db: FakeDB, ids):
        self.db, self.ids = db, list(ids)

    def __getitem__(self, i):
        return self.db.user(self.ids[i])

    def __iter__(self):
        return (self.db.user(i) for i in self.ids)

    def __len__(self):
        return len(self.ids)
