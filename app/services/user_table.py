"""Accounts (the old `users` table) as a DocTable on this node's relay (#161, wave 3).

One row per account: document `pcai:t:users:<id>`, row = every column of the old table except `id` (datetimes as
naive-UTC ISO strings), NIP-44-encrypted to the operator key like every DocTable -- the password hash, a linked
nsec and a pending Telegram link key are in there, so nothing here ever logs a row; only ids and field NAMES.

This is the AUTH PATH (sessions, API-key owners, Telegram, Nostr login, admin checks), so three rules on top of the
DocTable ones:

  * "COULD NOT ASK" IS NEVER "NO SUCH USER". Every reader raises `Unavailable` (the app answers 503 + Retry-After)
    when the relay could not be read. A session is never logged out as "user not found", and no
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

Relay-only from the first request: there is no SQL side (a node's old Postgres accounts are copied ONCE, ids and
all, by scripts/migrate_tables_to_nostr.py --table users).

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


# The old `users` table's columns (the copy script checks this list against the SQL model) and their defaults.
COLUMNS = ("username", "email", "password_hash", "is_admin", "email_verified", "notification_email", "avatar",
           "theme", "storage_quota", "can_image", "can_music", "can_video", "can_torrent", "can_media",
           "can_blossom", "can_stream", "can_ai", "access_revoked", "telegram_enabled", "telegram_chat_id",
           "telegram_notifications", "telegram_key", "telegram_key_expires_at", "nostr_enabled", "nostr_nsec",
           "nostr_npub", "nostr_relays", "nostr_media_service", "nostr_media_endpoint", "google_sub",
           "google_email", "stream_record", "pleroma_enabled", "pleroma_instance_url", "pleroma_acct",
           "social_notif_enabled", "nostr_notif_since", "tokens_valid_after", "created_at")
_DATETIMES = frozenset(("telegram_key_expires_at", "created_at"))
_DEFAULTS = {"is_admin": False, "email_verified": False, "theme": "cyberpunk", "storage_quota": 0,
             "can_image": True, "can_music": True, "can_video": True, "can_torrent": True, "can_media": False,
             "can_blossom": False, "can_stream": False, "can_ai": False, "access_revoked": False,
             "telegram_enabled": False, "telegram_notifications": "", "nostr_enabled": False,
             "stream_record": False, "pleroma_enabled": False, "social_notif_enabled": False}
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
    if _t().loaded:
        return _mem_by(field, values)
    return await _aremote_by(field, values)


def _by(db, field, value):
    values = _values(field, value)
    if not values:
        return None
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



def by_email(db, email):
    return _by(db, "email", email)


async def aby_npub(db, npub):
    """By the stored `nostr_npub` value (a string, or a list of spellings -- bech32 and hex)."""
    return await _aby(db, "nostr_npub", npub)


def by_npub(db, npub):
    return _by(db, "nostr_npub", npub)


async def aby_google_sub(db, sub):
    return await _aby(db, "google_sub", sub)



async def aby_telegram_key(db, key):
    return await _aby(db, "telegram_key", key)



async def aby_telegram_chat(db, chat_id, *, enabled_only: bool = True):
    """The account linked to this Telegram chat (and with Telegram switched on, unless `enabled_only=False`)."""
    u = await _aby(db, "telegram_chat_id", str(chat_id) if chat_id is not None else None)
    return u if u is not None and (u.telegram_enabled or not enabled_only) else None



# ---- whole-table reads
async def aall(db) -> list:
    """Every account, by id. Unavailable when the table could not be read -- never an empty list."""
    return _wrap(await table_gate.arows(_t()))


def all_users(db) -> list:
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


async def _write(uid, changes, **kw):
    return await _w_relay(uid, changes, **kw)


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
    uid, merged = await _submit(_write(None, _check_fields(fields), create=True))
    logger.info("[user-table] created account %s", uid)
    return UserRow(uid, merged)


def create(db=None, **fields) -> UserRow:
    uid, merged = _submit_sync(_write(None, _check_fields(fields), create=True))
    logger.info("[user-table] created account %s", uid)
    return UserRow(uid, merged)


async def asave(db, user: UserRow) -> UserRow:
    """Store the columns changed on `user` (merged onto the newest row); `user` is refreshed from the result."""
    ch = user.changes()
    if not ch:
        return user
    uid, merged = await _submit(_write(user.id, ch))
    return _done(user, uid, merged)


def save(db, user: UserRow) -> UserRow:
    ch = user.changes()
    if not ch:
        return user
    uid, merged = _submit_sync(_write(user.id, ch))
    return _done(user, uid, merged)


async def aupdate(db, user_id, **fields) -> UserRow:
    uid, merged = await _submit(_write(int(user_id), _check_fields(fields)))
    return UserRow(uid, merged)


def update(db, user_id, **fields) -> UserRow:
    uid, merged = _submit_sync(_write(int(user_id), _check_fields(fields)))
    return UserRow(uid, merged)


async def adelete(db, user_id) -> None:
    """Remove the account row (its other rows are the caller's: the old FK cascade lives in the services)."""
    await _submit(_write(int(user_id), {}, delete=True))
    logger.info("[user-table] deleted account %s", user_id)


def delete(db, user_id) -> None:
    _submit_sync(_write(int(user_id), {}, delete=True))
    logger.info("[user-table] deleted account %s", user_id)


# ================================================================== the copy script's index
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
