"""The managed bots (the old `bots` table, Admin -> Bots) as a DocTable on this node's relay (#161, wave 2).

One row per bot: document `pcai:t:bots:<id>`, row `{"name", "enabled", "bot_type", "platform", "host", "modes",
"config", "created_at", "updated_at"}` -- `config` stays the JSON STRING the column held, so `bot_to_dict`, the
admin form and every reader parse it exactly as before. A bot's config carries its secrets (the `nostr_nsec`, API
tokens, passwords); the document is NIP-44-encrypted to the operator key like every DocTable, and nothing here
logs a config -- only names and ids.

Ids are kept: a migrated bot keeps its SQL id (the admin UI's URLs carry it), a new one gets
`app_tables.new_id()`. The name stays unique, checked under a process lock (the bots are written by the admin
routes of the port-3051 process only).

Until the `bots` migration marker exists SQL is authoritative (table_gate): a new bot takes its id from the SQL
sequence, its relay document is written before the SQL commit, and every change goes to both. After it, only the
relay. The old per-name mirror (`pcai:bot:<name>`, bots_store) is written as before until the marker -- it is what
`bots_store.hydrate` seeds SQL from, and SQL is what the migration copies -- and not after.

Callers get `BotRow` objects with the old row's attributes (`b.enabled`, `b.config`, `b.host` ...).
"""
import logging
import threading
from datetime import datetime

from app.services import app_tables, table_gate
from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

TABLE = "bots"
FIELDS = ("name", "enabled", "bot_type", "platform", "host", "modes", "config")
_DEFAULTS = {"enabled": True, "bot_type": "text", "platform": "nostr", "host": None, "modes": "", "config": "{}"}
_write_lock = threading.RLock()


class NameTaken(ValueError):
    """Another bot already has this name."""


def _t() -> DocTable:
    return DocTable(TABLE)


class BotRow:
    __slots__ = ("id",) + FIELDS + ("created_at", "updated_at")

    def __init__(self, id=None, **kw):  # noqa: A002
        self.id = int(id) if id is not None else None
        for f in FIELDS:
            setattr(self, f, kw.get(f, _DEFAULTS.get(f)))
        self.enabled = bool(self.enabled) if self.enabled is not None else True
        self.created_at = app_tables.from_iso(kw.get("created_at")) or datetime.utcnow()
        self.updated_at = app_tables.from_iso(kw.get("updated_at")) or self.created_at

    def row(self) -> dict:
        r = {f: getattr(self, f) for f in FIELDS}
        r["enabled"] = bool(r["enabled"])
        r["created_at"] = app_tables.to_iso(self.created_at)
        r["updated_at"] = app_tables.to_iso(self.updated_at)
        return r

    @classmethod
    def from_row(cls, k, r: dict) -> "BotRow":
        return cls(int(k), **r)

    @classmethod
    def from_sql(cls, b) -> "BotRow":
        return cls(b.id, **{f: getattr(b, f) for f in FIELDS}, created_at=b.created_at, updated_at=b.updated_at)

    def __repr__(self):     # never the config: it holds the bot's keys
        return "BotRow(id=%s, name=%r)" % (self.id, self.name)


def sql_rows(db) -> dict:
    """{id: row} for the whole SQL table -- the migration's source."""
    from app.models import Bot
    return {str(b.id): BotRow.from_sql(b).row() for b in db.query(Bot).all()}


def _session(db):
    if db is not None:
        return db, False
    from app.database import SessionLocal
    return SessionLocal(), True


# ------------------------------------------------------------------ reads
def _by_name(bots, name):
    return next((b for b in bots if b.name == name), None)


def all_bots(db=None) -> list:
    """Every bot, by name. Unavailable when the relay could not be asked."""
    if not table_gate.relay_mode(TABLE):
        s, own = _session(db)
        try:
            from app.models import Bot
            out = [BotRow.from_sql(b) for b in s.query(Bot).all()]
        finally:
            if own:
                s.close()
    else:
        out = [BotRow.from_row(k, r) for k, r in table_gate.rows(_t())]
    return sorted(out, key=lambda b: (b.name or "", b.id or 0))


async def aall_bots(db=None) -> list:
    if not await table_gate.arelay_mode(TABLE):
        return all_bots(db)
    return sorted((BotRow.from_row(k, r) for k, r in await table_gate.arows(_t())),
                  key=lambda b: (b.name or "", b.id or 0))


def get(db, bot_id):
    try:
        bot_id = int(bot_id)
    except (TypeError, ValueError):
        return None
    if not table_gate.relay_mode(TABLE):
        s, own = _session(db)
        try:
            from app.models import Bot
            b = s.query(Bot).filter(Bot.id == bot_id).first()
            return BotRow.from_sql(b) if b is not None else None
        finally:
            if own:
                s.close()
    r = table_gate.get_row(_t(), str(bot_id))
    return BotRow.from_row(bot_id, r) if r is not None else None


async def aget(db, bot_id):
    try:
        bot_id = int(bot_id)
    except (TypeError, ValueError):
        return None
    if not await table_gate.arelay_mode(TABLE):
        return get(db, bot_id)
    r = await table_gate.aget_row(_t(), str(bot_id))
    return BotRow.from_row(bot_id, r) if r is not None else None


def get_by_name(db, name):
    return _by_name(all_bots(db), name) if name else None


async def aget_by_name(db, name):
    return _by_name(await aall_bots(db), name) if name else None


# ------------------------------------------------------------------ writes
def _legacy_mirror(db, bot: BotRow, old_name=None) -> None:
    """Before the marker the per-name relay mirror stays current (bots_store.hydrate seeds SQL from it)."""
    from app.services import bots_store
    if old_name and old_name != bot.name:
        bots_store.delete_bot_blocking(db, old_name)
    bots_store.sync_bot_blocking(db, bot)


def _check_name(bot: BotRow, bots) -> None:
    other = _by_name(bots, bot.name)
    if other is not None and other.id != bot.id:
        raise NameTaken("a bot named %r already exists" % bot.name)


def create(db, **fields) -> BotRow:
    """A new bot. Raises NameTaken, or Unavailable when the relay refused it (nothing is stored then)."""
    bot = BotRow(None, **fields)
    now = datetime.utcnow()
    bot.created_at = bot.updated_at = now
    with _write_lock:
        _check_name(bot, all_bots(db))
        if table_gate.relay_mode(TABLE):
            t = _t()
            bot.id = app_tables.new_id({k for k, _r in table_gate.rows(t)})
            t.put(str(bot.id), bot.row())
            return bot
        from app.models import Bot
        s, own = _session(db)
        try:
            b = Bot(**{f: getattr(bot, f) for f in FIELDS}, created_at=now, updated_at=now)
            s.add(b)
            s.flush()
            bot.id = int(b.id)
            _t().put(str(bot.id), bot.row())       # relay first ...
            s.commit()                            # ... then SQL
        except Exception:
            s.rollback()
            raise
        finally:
            if own:
                s.close()
        _legacy_mirror(db, bot)
        return bot


def save(db, bot: BotRow, *, old_name=None) -> BotRow:
    """Store a changed bot (every field). `old_name` = its name before this change (a rename drops the old
    per-name mirror before the marker). Raises NameTaken / Unavailable."""
    if bot.id is None:
        raise ValueError("save() needs a stored bot; use create()")
    bot.updated_at = datetime.utcnow()
    with _write_lock:
        _check_name(bot, all_bots(db))
        sql = not table_gate.relay_mode(TABLE)
        _t().put(str(bot.id), bot.row())
        if not sql:
            if old_name and old_name != bot.name:
                _drop_legacy_mirror(db, old_name)
            return bot
        from app.models import Bot
        s, own = _session(db)
        try:
            b = s.query(Bot).filter(Bot.id == bot.id).first()
            if b is None:
                b = Bot(id=bot.id, created_at=bot.created_at)
                s.add(b)
            for f in FIELDS:
                setattr(b, f, getattr(bot, f))
            b.updated_at = bot.updated_at
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            if own:
                s.close()
        _legacy_mirror(db, bot, old_name)
        return bot


def _drop_legacy_mirror(db, name) -> None:
    """A deleted or renamed bot's `pcai:bot:<name>` document goes even after the marker: an older build's
    hydrate would otherwise bring the bot back from it."""
    try:
        from app.services import bots_store
        bots_store.delete_bot_blocking(db, name)
    except Exception as e:      # noqa: BLE001
        logger.warning("[bot-table] legacy mirror for a removed bot not deleted: %s", type(e).__name__)


def delete(db, bot: BotRow) -> None:
    with _write_lock:
        sql = not table_gate.relay_mode(TABLE)
        _t().delete(str(bot.id))
        if sql:
            from app.models import Bot
            s, own = _session(db)
            try:
                s.query(Bot).filter(Bot.id == bot.id).delete()
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                if own:
                    s.close()
        _drop_legacy_mirror(db, bot.name)


async def asave(db, bot: BotRow, **kw) -> BotRow:
    """`save` from async code (the write path is synchronous and does relay I/O: run it off the loop)."""
    import asyncio
    return await asyncio.to_thread(save, db, bot, **kw)


__all__ = ["BotRow", "NameTaken", "Unavailable", "all_bots", "aall_bots", "get", "aget", "get_by_name",
           "aget_by_name", "create", "save", "asave", "delete", "sql_rows", "TABLE"]
