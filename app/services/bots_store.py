"""Bot config read-path → Nostr relay (Phase 3 of the Nostr-as-datastore migration).

SUPERSEDED by bot_table (#161, wave 2): the bots are now ONE operator DocTable keyed by id, and this
per-name mirror is no longer written for new bots (relay-only, no SQL); what is left here is its sync/delete
helpers (a renamed or deleted bot's old document is removed). What follows describes the pre-#161 arrangement.

Mirrors `settings_store`/`users_store`: the relay becomes authoritative for each bot's config, while
the SQLite `bots` table stays a fast local **read-through cache** (the bot_manager + admin UI keep
reading it unchanged). Bot config is operator data, so docs are operator-signed (`pcai:bot:<name>`).

  * (no hydrate: the bots are relay-only -- bot_table; these per-name docs are the DR backup copy)
  * `sync_bot(db, bot)`  — on create/update/toggle: write that bot's config through to the relay.
  * `delete_bot(db, n)`  — on delete (or rename): remove the relay doc.

The bot routes are synchronous, so `*_blocking` wrappers drive the coroutines with `asyncio.run`.
The relay is the only datastore (always on).
"""

import asyncio
import json
import logging
import re

from app.services import nostr_store as store
from app.services import settings_store as _ss  # reuse operator-key / port helpers

logger = logging.getLogger(__name__)

BOT_FIELDS = ("name", "enabled", "bot_type", "platform", "host", "modes", "config")


def enabled(db) -> bool:
    """The relay is the ONLY datastore — always on (legacy sqlite mode removed). The bots table is
    a hydrated read-cache."""
    return True


def _record(b) -> dict:
    return {f: getattr(b, f, None) for f in BOT_FIELDS}


async def sync_bot(db, bot, *, force: bool = False) -> bool:
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


def _vet(rec: dict) -> dict:
    """A RELAY DOC IS NOT A FORM POST, AND IT RE-SEEDS THE ROW ON EVERY BOOT.

    `bots.py:_vet_config` refuses a `concord_invite` holding a private key — but only on the two
    paths that go through a route. This one does not: `hydrate` writes whatever the operator-signed
    doc says straight onto the row, so a value stored before that guard existed (or by an older
    build, or by another node) is re-applied at every startup and a correction made in the UI is
    undone by the next restart. Measured on this deployment: a live bot whose `concord_invite` was
    byte-identical to its `nostr_nsec` — a PRIVATE KEY in the field the code hands to the CORD
    parser and exports as `CONCORD_INVITE`. Same shape as the legacy `settings` table re-seeding a
    deleted setting (CLAUDE.md): deleting it in one place looks like it worked.

    A hydrate must never RAISE — that would take out every other bot in the same pass — so the bad
    key is DROPPED and named. The bot then reports "listener ON but NO community invite saved",
    which is true and actionable, rather than carrying a credential down a path built for a room
    link. Nothing here echoes the value: it is a secret whichever field it landed in.
    """
    raw = rec.get("config")
    if not isinstance(raw, str) or "concord_invite" not in raw:
        return rec
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return rec
    if not isinstance(cfg, dict):
        return rec
    inv = str(cfg.get("concord_invite") or "").strip()
    if not inv:
        return rec
    if inv.startswith(("nsec1", "ncryptsec1")):
        why = "a private key, not a room link"
    elif not re.match(r"^(https?://|naddr1|cord:)", inv, re.I):
        why = "not an invite link"
    elif "#" not in inv:
        why = "an invite link whose # fragment (the room key) was cut off"
    else:
        return rec
    logger.warning("[bots-store] %s: the stored concord_invite is %s — dropping it rather than "
                   "writing it into CONCORD_INVITE. Paste the room's invite link in Admin -> Bots.",
                   rec.get("name"), why)
    cfg.pop("concord_invite", None)
    return {**rec, "config": json.dumps(cfg)}


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
