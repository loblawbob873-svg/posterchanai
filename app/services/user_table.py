"""Accounts (the old `users` table) as a DocTable on this node's relay (#161, wave 3).

One row per account: document `pcai:t:users:<id>`, row = every column of the old table except `id` (datetimes as
naive-UTC ISO strings), NIP-44-encrypted to the operator key like every DocTable -- the password hash, a linked
nsec and a pending Telegram link key are in there, so nothing here ever logs a row; only ids and field NAMES.

This is the AUTH PATH (sessions, API-key owners, Telegram, Nostr login, admin checks), so three rules on top of the
DocTable ones:

  * "COULD NOT ASK" IS NEVER "NO SUCH USER". Every reader raises `Unavailable` (the app answers 503 + Retry-After)
    when the relay or the marker could not be read. A session is never logged out as "user not found", and no
    login path ever creates an account because a lookup failed -- the exception reaches the caller instead of None.
  * A POINT LOOKUP NEVER WAITS FOR THE TABLE. While a restart is still loading it, a lookup by id reads that ONE
    document, and a lookup by username / npub / Telegram chat / Telegram link key / Google sub / email reads ONE
    index document (`pcai:t:user_index:<sha256(field, value)>` -> {"ids": [...]}) and then the account it names,
    checking that the account really holds that value (an index entry left by an interrupted write is a hint,
    never an answer). The d-tag is a HASH because d-tags are plaintext and these values include emails and a
    one-time link secret.
  * EVERY WRITE IS SERIALISED through one writer thread in this process, so the uniqueness checks (username,
    email, npub, Telegram chat, Google sub -- refused with `UserConflict`, as SQL refused a duplicate) and the
    index updates are atomic with the write, and two saves of the same account never lose each other's fields: a
    save carries only the fields the caller CHANGED (`UserRow` tracks them) and merges them onto the newest row.
    Index documents are written BEFORE the account (a value is never held by an account no index names), stale
    ones after it.

Ids are stable: a migrated account keeps its SQL id (sessions carry it, every other table refers to it); a new one
gets `app_tables.new_id()` -- unique across processes, never reusing a deleted account's id (its files, drive and
documents are keyed by it), far above every SQL id and inside a JavaScript number.

Until the `users` migration marker exists SQL is authoritative (table_gate, the wave-2 rule): reads come from SQL;
a write goes to the relay FIRST (index + account) and then commits in SQL (a create takes its id from the SQL
sequence, flushed, committed only after the relay holds it). After the marker only the relay is used.

Callers get `UserRow` objects carrying the old ORM attributes, so `user.is_admin`, `UserResponse.model_validate(user)`
and `user.theme = "x"; await user_table.asave(db, user)` read as before. The sync functions are for synchronous
code; ON the event loop a sync read is answered from memory once the table is loaded and raises Unavailable before
that (it can never block the loop on a socket), and a sync write raises RuntimeError (use the async one).
"""
import asyncio
import concurrent.futures
import hashlib
import logging
import threading
from datetime import datetime

from app.services import app_tables, table_gate
from app.services.doc_table import DocTable, _in_loop
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

TABLE = "users"
INDEX = "user_index"

# Fields an account can be found by (each has index documents), and the ones no two accounts may share.
INDEXED = ("username", "email", "nostr_npub", "telegram_chat_id", "telegram_key", "google_sub")
UNIQUE = ("username", "email", "nostr_npub", "telegram_chat_id", "google_sub")


class UserConflict(ValueError):
    """Another account already holds this username / email / npub / Telegram chat / Google sub."""

    def __init__(self, field: str):
        super().__init__("another account already has this %s" % field)
        self.field = field


class UserGone(LookupError):
    """The account being saved no longer exists."""


def _columns():
    from app.models import User
    cols, dts, defaults = [], set(), {}
    for c in User.__table__.columns:
        if c.name == "id":
            continue
        cols.append(c.name)
        if c.type.__class__.__name__ == "DateTime":
            dts.add(c.name)
        d = c.default
        if d is not None and not callable(d.arg):
            defaults[c.name] = d.arg
    return tuple(cols), frozenset(dts), defaults


COLUMNS, _DATETIMES, _DEFAULTS = _columns()
_COLSET = frozenset(COLUMNS)


def _t() -> DocTable:
    return DocTable(TABLE)


def _ix() -> DocTable:
    return DocTable(INDEX)


def index_key(field: str, value) -> str:
    return hashlib.sha256(("%s\0%s" % (field, value)).encode()).hexdigest()


def _empty(v) -> bool:
    return v is None or v == ""


def _store(name, v):
    if name in _DATETIMES:
        return app_tables.to_iso(v)
    return v


class UserRow:
    """One account, with the old `User` model's attributes. Assigning a column marks it changed; `asave` /
    `save` store exactly the changed columns."""

    def __init__(self, id, row: dict):        # noqa: A002
        object.__setattr__(self, "_dirty", set())
        object.__setattr__(self, "id", int(id) if id is not None else None)
        self._fill(row)

    def _fill(self, row: dict) -> None:
        for name in COLUMNS:
            v = row.get(name, _DEFAULTS.get(name))
            if name in _DATETIMES:
                v = app_tables.from_iso(v)
            object.__setattr__(self, name, v)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name in _COLSET:
            self._dirty.add(name)

    def row(self) -> dict:
        return {name: _store(name, getattr(self, name)) for name in COLUMNS}

    def changes(self) -> dict:
        return {name: _store(name, getattr(self, name)) for name in self._dirty}

    @classmethod
    def from_row(cls, k, r: dict) -> "UserRow":
        return cls(int(k), r)

    @classmethod
    def from_sql(cls, u) -> "UserRow":
        return cls(u.id, {name: getattr(u, name, None) for name in COLUMNS})

    def __eq__(self, other):
        return isinstance(other, UserRow) and other.id == self.id

    def __hash__(self):
        return hash(("user", self.id))

    def __repr__(self):
        return "UserRow(id=%s, username=%r)" % (self.id, self.username)


# ================================================================== reads
def _wrap(pairs) -> list:
    return sorted((UserRow.from_row(k, r) for k, r in pairs), key=lambda u: u.id)


def _matches(row: dict, field: str, values) -> bool:
    return row is not None and row.get(field) in values


def _values(field, value) -> list:
    """The lookup value(s) as stored: ids as ints, every other indexed column is a string column."""
    vals = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    vals = [v for v in vals if not _empty(v)]
    if field == "id":
        return [int(v) for v in vals]
    return [str(v) for v in vals]


# ---- SQL side (authoritative until the marker)
def _sql_query(db):
    from app.models import User
    return db.query(User).order_by(User.id.asc())


def _sql_by(db, field, values):
    from app.models import User
    if field == "id":
        return _sql_query(db).filter(User.id.in_([int(v) for v in values])).first()
    return _sql_query(db).filter(getattr(User, field).in_(list(values))).first()


def _with_db(db, fn):
    if db is not None:
        return fn(db)
    from app import database
    s = database.SessionLocal()
    try:
        return fn(s)
    finally:
        s.close()


# ---- relay side
def _mem_by(field, values):
    t = _t()
    if field == "id":
        for v in values:
            r = t.peek(str(int(v)))
            if r is not None:
                return UserRow.from_row(int(v), r)
        return None
    best = None
    for k, r in t.rows_view():
        if r.get(field) in values and (best is None or int(k) < best[0]):
            best = (int(k), r)
    return UserRow.from_row(*best) if best else None


async def _aremote_by(field, values):
    """Before the table is loaded: the index document, then the account(s) it names, verified."""
    if field == "id":
        for v in values:
            r = await _t().aget_remote(str(int(v)))
            if r is not None:
                return UserRow.from_row(int(v), r)
        return None
    ids = set()
    for v in values:
        ix = await _ix().aget_remote(index_key(field, v))
        if ix:
            ids.update(int(i) for i in ix.get("ids") or [])
    for uid in sorted(ids):
        r = await _t().aget_remote(str(uid))
        if _matches(r, field, values):
            return UserRow.from_row(uid, r)
    return None


_bg = {"loading": False}


def _load_in_background() -> None:
    """Load the table on a thread of its own (the next lookup is then answered from memory)."""
    if _bg["loading"]:
        return
    _bg["loading"] = True

    def _go():
        try:
            _t().load()
        except Exception as e:      # noqa: BLE001 -- the next lookup tries again
            logger.debug("[user-table] background load failed: %s", e)
        finally:
            _bg["loading"] = False
    threading.Thread(target=_go, name="user-table-load", daemon=True).start()


async def _aby(db, field, value):
    values = _values(field, value)
    if not values:
        return None
    if not await table_gate.arelay_mode(TABLE):
        r = _with_db(db, lambda s: _sql_by(s, field, values))
        return UserRow.from_sql(r) if r is not None else None
    if _t().loaded:
        return _mem_by(field, values)
    return await _aremote_by(field, values)


def _by(db, field, value):
    values = _values(field, value)
    if not values:
        return None
    if not table_gate.relay_mode(TABLE):
        r = _with_db(db, lambda s: _sql_by(s, field, values))
        return UserRow.from_sql(r) if r is not None else None
    if _t().loaded:
        return _mem_by(field, values)
    if _in_loop():
        table_gate._load_soon(_t())
        raise Unavailable("the users table is still loading")
    _load_in_background()
    from app.services.doc_table import _run
    return _run(_aremote_by(field, values))


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---- public point lookups (None = the store answered and holds no such account)
async def aget(db, user_id):
    uid = _int_or_none(user_id)
    return None if uid is None else await _aby(db, "id", uid)


def get(db, user_id):
    uid = _int_or_none(user_id)
    return None if uid is None else _by(db, "id", uid)


async def aby_username(db, username):
    return await _aby(db, "username", username)


def by_username(db, username):
    return _by(db, "username", username)


async def aby_email(db, email):
    return await _aby(db, "email", email)


def by_email(db, email):
    return _by(db, "email", email)


async def aby_npub(db, npub):
    """By the stored `nostr_npub` value (a string, or a list of spellings -- bech32 and hex)."""
    return await _aby(db, "nostr_npub", npub)


def by_npub(db, npub):
    return _by(db, "nostr_npub", npub)


async def aby_google_sub(db, sub):
    return await _aby(db, "google_sub", sub)


def by_google_sub(db, sub):
    return _by(db, "google_sub", sub)


async def aby_telegram_key(db, key):
    return await _aby(db, "telegram_key", key)


def by_telegram_key(db, key):
    return _by(db, "telegram_key", key)


async def aby_telegram_chat(db, chat_id, *, enabled_only: bool = True):
    """The account linked to this Telegram chat (and with Telegram switched on, unless `enabled_only=False`)."""
    u = await _aby(db, "telegram_chat_id", str(chat_id) if chat_id is not None else None)
    return u if u is not None and (u.telegram_enabled or not enabled_only) else None


def by_telegram_chat(db, chat_id, *, enabled_only: bool = True):
    u = _by(db, "telegram_chat_id", str(chat_id) if chat_id is not None else None)
    return u if u is not None and (u.telegram_enabled or not enabled_only) else None


# ---- whole-table reads
async def aall(db) -> list:
    """Every account, by id. Unavailable when the table could not be read -- never an empty list."""
    if not await table_gate.arelay_mode(TABLE):
        return _with_db(db, lambda s: [UserRow.from_sql(u) for u in _sql_query(s).all()])
    return _wrap(await table_gate.arows(_t()))


def all_users(db) -> list:
    if not table_gate.relay_mode(TABLE):
        return _with_db(db, lambda s: [UserRow.from_sql(u) for u in _sql_query(s).all()])
    return _wrap(table_gate.rows(_t()))


async def afilter(db, pred) -> list:
    return [u for u in await aall(db) if pred(u)]


def filter_users(db, pred) -> list:
    return [u for u in all_users(db) if pred(u)]


async def aadmins(db) -> list:
    return await afilter(db, lambda u: bool(u.is_admin))


def admins(db) -> list:
    return filter_users(db, lambda u: bool(u.is_admin))


async def aoperator(db):
    """The first admin account holding a linked nsec (the node operator's own account), or None."""
    for u in await aadmins(db):
        if u.nostr_nsec:
            return u
    return None


def operator(db):
    for u in admins(db):
        if u.nostr_nsec:
            return u
    return None


async def aany_admin(db) -> bool:
    return bool(await aadmins(db))


def any_admin(db) -> bool:
    return bool(admins(db))


async def afree_username(db, base: str, *, limit: int = 1000, width: int = 50) -> str:
    """`base`, or `base2`, `base3` ... -- the first no account holds (the loop every sign-up path ran)."""
    taken = {u.username for u in await aall(db)}
    return _free(base, taken, limit, width)


def free_username(db, base: str, *, limit: int = 1000, width: int = 50) -> str:
    taken = {u.username for u in all_users(db)}
    return _free(base, taken, limit, width)


def _free(base, taken, limit, width):
    base = (base or "user")[:width]
    if base not in taken:
        return base
    for i in range(2, limit):
        cand = "%s%d" % (base[:width - len(str(i))], i)
        if cand not in taken:
            return cand
    raise UserConflict("username")


# ================================================================== writes (one writer thread)
class _Writer:
    def __init__(self):
        self._loop = None
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            if self._loop is None or not self._thread.is_alive():
                ready = threading.Event()

                def _run():
                    self._loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._loop)
                    ready.set()
                    self._loop.run_forever()
                self._thread = threading.Thread(target=_run, name="user-table-writer", daemon=True)
                self._thread.start()
                ready.wait()
        return self._loop

    def submit(self, coro) -> concurrent.futures.Future:
        loop = self._ensure()
        return asyncio.run_coroutine_threadsafe(_serial(coro), loop)


_writer = _Writer()
_wlock: dict = {}


async def _serial(coro):
    lk = _wlock.get("lk")
    if lk is None:
        lk = _wlock["lk"] = asyncio.Lock()
    async with lk:
        return await coro


async def _submit(coro):
    return await asyncio.wrap_future(_writer.submit(coro))


def _submit_sync(coro):
    if _in_loop():
        coro.close()
        raise RuntimeError("user_table sync write on an event-loop thread -- use the async function")
    return _writer.submit(coro).result()


# ---- relay mode
def _holders(field, value, rows) -> set:
    return {int(k) for k, r in rows if r.get(field) == value}


async def _put_index(field, value, ids) -> None:
    k = index_key(field, value)
    if ids:
        await _ix().aput(k, {"field": field, "ids": sorted(ids)})
    else:
        try:
            await _ix().adelete(k)
        except Unavailable:
            # a stale index entry is harmless (every hit is verified against the account) -- but say so
            logger.warning("[user-table] could not remove a stale %s index entry", field)


async def _w_relay(uid, changes: dict, *, create: bool = False, delete: bool = False):
    t = _t()
    await t._ensure()                              # strict: uniqueness is decided on the WHOLE table
    rows = t.rows_view()
    if create:
        uid = app_tables.new_id({k for k, _r in rows})
        cur = {}
        merged = {name: _DEFAULTS.get(name) for name in COLUMNS}
        merged["created_at"] = app_tables.to_iso(datetime.utcnow())
        merged.update(changes)
    else:
        cur = t.peek(str(uid))
        if cur is None:
            raise UserGone("no account %s" % uid)
        merged = dict(cur)
        merged.update(changes)
    others = [(k, r) for k, r in rows if int(k) != uid]
    if delete:
        await t.adelete(str(uid))
        for f in INDEXED:
            if not _empty(cur.get(f)):
                await _put_index(f, cur[f], _holders(f, cur[f], others))
        return uid, None
    for f in UNIQUE:
        v = merged.get(f)
        if not _empty(v) and (create or cur.get(f) != v) and _holders(f, v, others):
            raise UserConflict(f)
    moved = [f for f in INDEXED if merged.get(f) != cur.get(f)]
    for f in moved:
        if not _empty(merged.get(f)):
            await _put_index(f, merged[f], _holders(f, merged[f], others) | {uid})
    await t.aput(str(uid), merged)
    for f in moved:
        if not _empty(cur.get(f)):
            await _put_index(f, cur[f], _holders(f, cur[f], others))
    return uid, merged


# ---- SQL mode (relay first, then the SQL commit)
def _sql_holders(s, field, value, exclude=None) -> set:
    from app.models import User
    q = s.query(User.id).filter(getattr(User, field) == value)
    return {int(i) for (i,) in q.all() if i != exclude}


def _bind_of(db):
    """The engine the caller's session talks to: a SQL-mode write commits in the SAME database the caller reads
    (never a default one), on a session of its own (the writer thread's)."""
    try:
        return db.get_bind() if db is not None else None
    except Exception:      # noqa: BLE001
        return None


def _session(bind):
    if bind is not None:
        from sqlalchemy.orm import Session
        return Session(bind=bind, autoflush=False)
    from app import database
    return database.SessionLocal()


async def _w_sql(uid, changes: dict, *, create: bool = False, delete: bool = False, bind=None):
    from app.models import User
    s = _session(bind)
    try:
        if create:
            for f in UNIQUE:
                if not _empty(changes.get(f)) and _sql_holders(s, f, changes[f]):
                    raise UserConflict(f)
            u = User(**{k: (app_tables.from_iso(v) if k in _DATETIMES else v) for k, v in changes.items()})
            s.add(u)
            s.flush()                               # the SQL sequence gives the id; committed after the relay
            uid = int(u.id)
            cur = {}
        else:
            u = s.query(User).filter(User.id == int(uid)).first()
            if u is None:
                raise UserGone("no account %s" % uid)
            cur = UserRow.from_sql(u).row()
        merged = UserRow.from_sql(u).row() if create else dict(cur, **changes)
        if delete:
            await _t().adelete(str(uid))
            s.delete(u)
            s.commit()
            for f in INDEXED:
                if not _empty(cur.get(f)):
                    await _put_index(f, cur[f], _sql_holders(s, f, cur[f], exclude=uid))
            return uid, None
        for f in UNIQUE:
            v = merged.get(f)
            if not _empty(v) and (create or cur.get(f) != v) and _sql_holders(s, f, v, exclude=uid):
                raise UserConflict(f)
        moved = [f for f in INDEXED if merged.get(f) != cur.get(f)]
        for f in moved:
            if not _empty(merged.get(f)):
                await _put_index(f, merged[f], _sql_holders(s, f, merged[f], exclude=uid) | {uid})
        await _t().aput(str(uid), merged)
        if not create:
            for k, v in changes.items():
                setattr(u, k, app_tables.from_iso(v) if k in _DATETIMES else v)
        s.commit()
        for f in moved:
            if not _empty(cur.get(f)):
                await _put_index(f, cur[f], _sql_holders(s, f, cur[f], exclude=uid))
        return uid, merged
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


async def _write(uid, changes, *, bind=None, **kw):
    if await table_gate.arelay_mode(TABLE):
        return await _w_relay(uid, changes, **kw)
    return await _w_sql(uid, changes, bind=bind, **kw)


def _check_fields(fields: dict) -> dict:
    bad = [k for k in fields if k not in _COLSET]
    if bad:
        raise TypeError("not a users column: %s" % ", ".join(sorted(bad)))
    return {k: _store(k, v) for k, v in fields.items()}


def _done(user, uid, merged):
    if user is None:
        return UserRow(uid, merged)
    object.__setattr__(user, "id", uid)
    user._fill(merged)
    user._dirty.clear()
    return user


async def acreate(db=None, **fields) -> UserRow:
    """A new account. Raises UserConflict when a unique field is already held, Unavailable when the relay could
    not be asked (nothing is created)."""
    uid, merged = await _submit(_write(None, _check_fields(fields), create=True, bind=_bind_of(db)))
    logger.info("[user-table] created account %s", uid)
    return UserRow(uid, merged)


def create(db=None, **fields) -> UserRow:
    uid, merged = _submit_sync(_write(None, _check_fields(fields), create=True, bind=_bind_of(db)))
    logger.info("[user-table] created account %s", uid)
    return UserRow(uid, merged)


async def asave(db, user: UserRow) -> UserRow:
    """Store the columns changed on `user` (merged onto the newest row); `user` is refreshed from the result."""
    ch = user.changes()
    if not ch:
        return user
    uid, merged = await _submit(_write(user.id, ch, bind=_bind_of(db)))
    return _done(user, uid, merged)


def save(db, user: UserRow) -> UserRow:
    ch = user.changes()
    if not ch:
        return user
    uid, merged = _submit_sync(_write(user.id, ch, bind=_bind_of(db)))
    return _done(user, uid, merged)


async def aupdate(db, user_id, **fields) -> UserRow:
    uid, merged = await _submit(_write(int(user_id), _check_fields(fields), bind=_bind_of(db)))
    return UserRow(uid, merged)


def update(db, user_id, **fields) -> UserRow:
    uid, merged = _submit_sync(_write(int(user_id), _check_fields(fields), bind=_bind_of(db)))
    return UserRow(uid, merged)


async def adelete(db, user_id) -> None:
    """Remove the account row (its other rows are the caller's: the old FK cascade lives in the services)."""
    await _submit(_write(int(user_id), {}, delete=True, bind=_bind_of(db)))
    logger.info("[user-table] deleted account %s", user_id)


def delete(db, user_id) -> None:
    _submit_sync(_write(int(user_id), {}, delete=True, bind=_bind_of(db)))
    logger.info("[user-table] deleted account %s", user_id)


# ================================================================== migration source
def sql_rows(db) -> dict:
    """{id: row} for the whole SQL table -- the migration's source."""
    return {str(u.id): UserRow.from_sql(u).row() for u in _sql_query(db).all()}


def index_rows(rows: dict) -> dict:
    """The index documents a set of account rows implies: {index_key: {"field", "ids"}}."""
    out = {}
    for k, r in rows.items():
        for f in INDEXED:
            v = r.get(f)
            if not _empty(v):
                e = out.setdefault(index_key(f, v), {"field": f, "ids": []})
                e["ids"].append(int(k))
    for e in out.values():
        e["ids"].sort()
    return out
