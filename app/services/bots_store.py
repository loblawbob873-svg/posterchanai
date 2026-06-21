"""Bot config read-path → Nostr relay (Phase 3 of the Nostr-as-datastore migration).

Mirrors `settings_store`/`users_store`: the relay becomes authoritative for each bot's config, while
the SQLite `bots` table stays a fast local **read-through cache** (the bot_manager + admin UI keep
reading it unchanged). Bot config is operator data, so docs are operator-signed (`pcai:bot:<name>`).

  * `hydrate(db)`        — at startup: UPSERT a `Bot` row for every `pcai:bot:<name>` doc.
  * `sync_bot(db, bot)`  — on create/update/toggle: write that bot's config through to the relay.
  * `delete_bot(db, n)`  — on delete (or rename): remove the relay doc.

The bot routes are synchronous, so `*_blocking` wrappers drive the coroutines with `asyncio.run`.
The relay is the only datastore (always on).
"""

import asyncio
import logging

from app.models import Bot
from app.services import nostr_store as store
from app.services import settings_store as _ss  # reuse operator-key / port helpers

logger = logging.getLogger(__name__)

BOT_FIELDS = ("name", "enabled", "bot_type", "platform", "host", "modes", "config")


def enabled(db) -> bool:
    """The relay is the ONLY datastore — always on (legacy sqlite mode removed). The bots table is
    a hydrated read-cache."""
    return True


def _record(b: Bot) -> dict:
    return {f: getattr(b, f, None) for f in BOT_FIELDS}


async def sync_bot(db, bot: Bot, *, force: bool = False) -> bool:
    """Write one bot's config through to the relay. No-op when disabled (unless `force`) / no
    operator key / no name. Returns True on success."""
    if bot is None or not bot.name or (not force and not enabled(db)):
        return False
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return False
    try:
        ok = await store.put_doc(_ss._port(db), op_sk, store.NS_BOT + bot.name, _record(bot))
        if ok:
            logger.info("[bots-store] synced bot %s to relay", bot.name)
        return ok
    except Exception as e:
        logger.warning("[bots-store] sync_bot failed for %s: %s", bot.name, e)
        return False


async def delete_bot(db, name: str, *, force: bool = False) -> bool:
    """Remove a bot's relay doc (on delete or rename). No-op when disabled (unless `force`)."""
    if not name or (not force and not enabled(db)):
        return False
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return False
    try:
        return await store.delete_doc(_ss._port(db), op_sk, store.NS_BOT + name)
    except Exception as e:
        logger.warning("[bots-store] delete_bot failed for %s: %s", name, e)
        return False


def _apply(db, rec: dict) -> bool:
    """UPSERT a Bot row from a relay config record (keyed by name). Returns True if changed."""
    name = rec.get("name")
    if not name:
        return False
    b = db.query(Bot).filter(Bot.name == name).first()
    created = b is None
    if created:
        b = Bot(name=name)
        db.add(b)
    changed = created
    for f in BOT_FIELDS:
        if f == "name":
            continue
        if f in rec and getattr(b, f, None) != rec[f]:
            setattr(b, f, rec[f])
            changed = True
    return changed


async def hydrate(db) -> int:
    """relay → bots cache. UPSERT a Bot row for every operator-signed bot doc. No-op when there's no
    operator key. Returns the number created-or-updated."""
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        logger.info("[bots-store] hydrate skipped — no operator key")
        return 0
    try:
        docs = await store.list_docs(_ss._port(db), store.NS_BOT, seckey=op_sk)
    except Exception as e:
        logger.warning("[bots-store] hydrate failed to read relay: %s", e)
        return 0
    changed = 0
    for _d, value in (docs or {}).items():
        rec = value.get("value") if isinstance(value, dict) and "value" in value else value
        if isinstance(rec, dict) and _apply(db, rec):
            changed += 1
    if changed:
        db.commit()
    logger.info("[bots-store] hydrated %d bot(s) from relay", changed)
    return changed


# ----- sync wrappers for the synchronous bot routes -----
def sync_bot_blocking(db, bot) -> None:
    try:
        if enabled(db):
            asyncio.run(sync_bot(db, bot))
    except Exception as e:
        logger.warning("[bots-store] sync_bot_blocking failed: %s", e)


def delete_bot_blocking(db, name: str) -> None:
    try:
        if enabled(db):
            asyncio.run(delete_bot(db, name))
    except Exception as e:
        logger.warning("[bots-store] delete_bot_blocking failed: %s", e)
