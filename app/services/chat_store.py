"""Phase 2: chat history as encrypted, user-deletable Nostr events (docs/NOSTR_DATASTORE.md).

Each message is a kind-30078 doc `d=pcai:msg:<conv>:<seq>`, **NIP-44-encrypted to the user's
server-held storage key** and signed by it — so it's never on a timeline, only the user (server,
on their behalf) can read it, and it's individually deletable via NIP-09 (`delete_doc`). The
conversation index (ids/titles) is also mirrored (see mirror_conversation); the *messages* live here.

The relay is the only datastore (always on): routers/services call add/get/delete, and every
committed Message row is auto-mirrored by the install_message_mirror() ORM hook below.
"""

import os
import time
import logging

from . import nostr_store as store
from .nostr_store import user_storage_seckey
from app.services import settings_store

logger = logging.getLogger(__name__)


def _port(db=None) -> int:
    return settings_store.get_int("nostr_relay_port", 3052)


def enabled(db) -> bool:
    """The relay is the ONLY datastore — always on (legacy sqlite mode removed)."""
    return True


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
    """Delete (NIP-09) every message event of a conversation — and the generated-artifact blobs the
    messages reference (image_path enc_<sha>) — so deleting a chat cleans up its files too."""
    import re as _re
    from . import artifact_store
    sk = user_storage_seckey(db, user)
    port = _port(db)
    # Remove referenced artifact blobs from Blossom. An artifact is referenced in one of TWO places
    # and only the first used to be cleaned: a generated IMAGE lands in `image_path`, but generated or
    # derived MEDIA (an extracted MP3, a rendered song/video, an agent's workspace backup, a captured
    # command output) is appended into the message CONTENT as a markdown link. So every one of those
    # survived its own chat's deletion, unreferenced and unlistable — that is where a multi-GB pile of
    # orphaned private blobs came from. Scan both, exactly like the Files listing does.
    for m in await get_messages(db, user, conv_id):
        shas = set(_re.findall(r'enc_([0-9a-f]{64})', m.get("image_path") or ""))
        shas |= set(_re.findall(r'enc_([0-9a-f]{64})', m.get("content") or ""))
        for sha in shas:
            try:
                await artifact_store.delete_blob(db, sha)
            except Exception:
                pass
    docs = await store.list_docs(port, f"{store.NS_MSG}{conv_id}:", seckey=sk, encrypt=False)
    removed = 0
    for d in docs.keys():
        try:
            if await store.delete_doc(port, sk, d):
                removed += 1
        except Exception as e:
            logger.warning("[chat-store] delete %s failed: %s", d, e)
    # drop the conversation index doc too
    try:
        await store.delete_doc(port, sk, f"{store.NS_CONV}{conv_id}")
    except Exception as e:
        logger.warning("[chat-store] delete conv index %s failed: %s", conv_id, e)
    return removed


# ---- conversation index (title/timestamps) as a per-user encrypted doc, so the chat LIST survives
# on a fresh node, not just the messages. Keyed by the SQLite conv id (the d-tag suffix). ----
async def mirror_conversation(db, user, conv) -> bool:
    """Write/replace a conversation's index doc (title + timestamps), encrypted to the user's key."""
    if conv is None:
        return False
    try:
        sk = user_storage_seckey(db, user)
        rec = {"title": conv.title or "New Chat",
               "created_at": conv.created_at.isoformat() if conv.created_at else None,
               "updated_at": conv.updated_at.isoformat() if conv.updated_at else None}
        return await store.put_doc(_port(db), sk, f"{store.NS_CONV}{conv.id}", rec)
    except Exception as e:
        logger.warning("[chat-store] mirror_conversation %s failed: %s", getattr(conv, "id", "?"), e)
        return False


async def hydrate_conversations(db) -> int:
    """relay → conversations cache. Recreate missing Conversation rows for every user from their
    NS_CONV docs (so a fresh node restores the chat list). Additive only — never edits existing rows.
    Returns the number of conversations recreated."""
    from datetime import datetime
    from app.models import Conversation, User
    made = 0
    for user in db.query(User).filter(User.nostr_npub.isnot(None)).all():
        try:
            sk = user_storage_seckey(db, user)
            docs = await store.list_docs(_port(db), store.NS_CONV, seckey=sk)
        except Exception:
            continue
        for d_tag, rec in (docs or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                conv_id = int(d_tag[len(store.NS_CONV):])
            except (TypeError, ValueError):
                continue
            if db.query(Conversation).filter(Conversation.id == conv_id).first():
                continue
            def _dt(v):
                try:
                    return datetime.fromisoformat(v) if v else None
                except (TypeError, ValueError):
                    return None
            db.add(Conversation(id=conv_id, user_id=user.id, title=rec.get("title") or "New Chat",
                                created_at=_dt(rec.get("created_at")), updated_at=_dt(rec.get("updated_at"))))
            made += 1
    if made:
        db.commit()
    logger.info("[chat-store] hydrated %d conversation(s) from relay", made)
    return made


# ---- automatic mirror: every Message row insert → an encrypted relay event (when flag on) ----
# Covers all the scattered Message save sites without editing each — both the async chat WS (scheduled
# on the live loop) AND off-path saves (APScheduler/Telegram threadpool, sync routes), which mirror on
# a short-lived daemon thread (see _after_commit). Non-nostr (no-npub) users are skipped.
# Strong refs to in-flight mirror tasks. asyncio only keeps a WEAK ref to a bare create_task(),
# so without this the task can be garbage-collected before its relay write lands — the bug where a
# chat reply (e.g. a flashcards deck) randomly fails to persist and is gone on reload.
_PENDING_MIRRORS: set = set()


async def _mirror_insert(conv_id: int, role: str, content: str, ts: float, image_path: str | None):
    from app.database import SessionLocal
    from app.models import Conversation, User
    db = SessionLocal()
    try:
        conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
        if not conv:
            return
        user = db.query(User).filter(User.id == conv.user_id).first()
        if user and user.nostr_npub:   # only nostr accounts have a storage key to encrypt under
            await add_message(db, user, conv_id, role, content, ts=ts, image_path=image_path)
    except Exception as e:
        logger.warning("[chat-store] mirror failed for conv %s (%s): %s", conv_id, role, e)
    finally:
        db.close()


def install_message_mirror():
    """Register the message mirror once (called at import).

    Mirror on **after_commit**, not after_insert: after_insert fires during flush (before COMMIT),
    so if a flush succeeds but the COMMIT then fails and the caller retries the save (see chat.py's
    assistant-save retry), the same message would mirror to the relay TWICE with distinct d-tags
    (no dedup) → a duplicate reply on reload. Staging the inserted rows per-session and flushing them
    to the relay only on commit (and dropping them on rollback) guarantees one relay doc per
    committed message."""
    import asyncio, time as _t
    from sqlalchemy import event
    from sqlalchemy.orm import Session, object_session
    from app.models import Message

    @event.listens_for(Message, "after_insert")
    def _after_insert(mapper, connection, target):   # noqa: ARG001
        sess = object_session(target)
        if sess is None or not target.conversation_id or not target.role:
            return
        sess.info.setdefault("_chat_mirror", []).append(
            (target.conversation_id, target.role, target.content or "", getattr(target, "image_path", None)))

    @event.listens_for(Session, "after_commit")
    def _after_commit(sess):
        rows = sess.info.pop("_chat_mirror", None)
        if not rows:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # In the async chat WS path: schedule on the live loop. Hold a strong ref until done —
            # see _PENDING_MIRRORS above (weak-ref GC footgun).
            for conv_id, role, content, image_path in rows:
                t = loop.create_task(_mirror_insert(conv_id, role, content, _t.time(), image_path))
                _PENDING_MIRRORS.add(t)
                t.add_done_callback(_PENDING_MIRRORS.discard)
            return
        # No running loop — a save off the async path (APScheduler/Telegram threadpool, sync route).
        # Mirror in a short-lived daemon thread so the committing thread isn't blocked on relay I/O,
        # and ALL message save sites get covered without per-site wiring.
        import threading
        rows_snapshot = list(rows)
        def _run():
            for conv_id, role, content, image_path in rows_snapshot:
                try:
                    asyncio.run(_mirror_insert(conv_id, role, content, _t.time(), image_path))
                except Exception as e:
                    logger.warning("[chat-store] threaded mirror failed for conv %s: %s", conv_id, e)
        threading.Thread(target=_run, name="chat-mirror", daemon=True).start()

    @event.listens_for(Session, "after_rollback")
    def _after_rollback(sess):
        sess.info.pop("_chat_mirror", None)   # rolled-back inserts must NOT mirror


try:
    install_message_mirror()
except Exception as _e:   # pragma: no cover
    logger.warning("[chat-store] could not install message mirror: %s", _e)
