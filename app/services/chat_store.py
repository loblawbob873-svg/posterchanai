"""Phase 2: chat history as encrypted, user-deletable Nostr events (docs/NOSTR_DATASTORE.md).

Each message is a kind-30078 doc `d=pcai:msg:<conv>:<seq>`, **NIP-44-encrypted to the user's
server-held storage key** and signed by it — so it's never on a timeline, only the user (server,
on their behalf) can read it, and it's individually deletable via NIP-09 (`delete_doc`). The
conversation index (ids/titles) stays in SQLite for now; the *messages* live here.

Wiring into the live chat path is incremental + flag-gated (`chat_backend` setting = `relay`): this
module is the store; routers/services call add/get/delete. SQLite message rows remain until every
save point is routed here, then they're retired.
"""

import os
import time
import logging

from . import nostr_store as store
from .nostr_store import user_storage_seckey

logger = logging.getLogger(__name__)


def _port(db) -> int:
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == "nostr_relay_port").first()
    return int(row.value) if row and row.value else 3052


def enabled(db) -> bool:
    """True when chats should read/write the relay store (setting chat_backend == 'relay')."""
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == "chat_backend").first()
    return bool(row and (row.value or "").strip().lower() == "relay")


async def add_message(db, user, conv_id: int, role: str, content: str, ts: float | None = None,
                      image_path: str | None = None) -> bool:
    """Append one chat message as an encrypted event. `seq` (ms + rand) keeps ordering + a unique d.
    `image_path` (a stored artifact like a generated image) is carried so it survives reload."""
    sk = user_storage_seckey(db, user)
    ts = ts if ts is not None else time.time()
    d = f"{store.NS_MSG}{conv_id}:{int(ts * 1000):015d}-{os.urandom(2).hex()}"
    rec = {"conv": conv_id, "role": role, "content": content, "ts": ts}
    if image_path:
        rec["image_path"] = image_path
    return await store.put_doc(_port(db), sk, d, rec)


async def get_messages(db, user, conv_id: int) -> list:
    """All messages for a conversation, oldest first, as [{role, content, ts}]."""
    sk = user_storage_seckey(db, user)
    docs = await store.list_docs(_port(db), f"{store.NS_MSG}{conv_id}:", seckey=sk)
    msgs = [v for v in docs.values() if isinstance(v, dict) and "role" in v]
    msgs.sort(key=lambda m: m.get("ts", 0))
    return msgs


async def delete_conversation(db, user, conv_id: int) -> int:
    """Delete (NIP-09) every message event of a conversation — the user's own key signs the delete."""
    sk = user_storage_seckey(db, user)
    port = _port(db)
    docs = await store.list_docs(port, f"{store.NS_MSG}{conv_id}:", seckey=sk, encrypt=False)
    removed = 0
    for d in docs.keys():
        try:
            if await store.delete_doc(port, sk, d):
                removed += 1
        except Exception as e:
            logger.warning("[chat-store] delete %s failed: %s", d, e)
    return removed


# ---- automatic mirror: every Message row insert → an encrypted relay event (when flag on) ----
# Covers all the scattered save sites in the chat path without editing each. Best-effort: only fires
# inside the async chat WS (a running loop); bot/threadpool saves aren't part of the web AI store.
async def _mirror_insert(conv_id: int, role: str, content: str, ts: float, image_path: str | None):
    from app.database import SessionLocal
    from app.models import Conversation, User
    db = SessionLocal()
    try:
        if not enabled(db):
            return
        conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
        if not conv:
            return
        user = db.query(User).filter(User.id == conv.user_id).first()
        if user:
            await add_message(db, user, conv_id, role, content, ts=ts, image_path=image_path)
    except Exception as e:
        logger.debug("[chat-store] mirror failed: %s", e)
    finally:
        db.close()


def install_message_mirror():
    """Register the after_insert listener once (called at import)."""
    import asyncio
    from sqlalchemy import event
    from app.models import Message

    @event.listens_for(Message, "after_insert")
    def _after_insert(mapper, connection, target):   # noqa: ARG001
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # not in the async chat path — skip
        import time as _t
        conv_id, role, content = target.conversation_id, target.role, target.content or ""
        image_path = getattr(target, "image_path", None)
        if conv_id and role:
            loop.create_task(_mirror_insert(conv_id, role, content, _t.time(), image_path))


try:
    install_message_mirror()
except Exception as _e:   # pragma: no cover
    logger.warning("[chat-store] could not install message mirror: %s", _e)
