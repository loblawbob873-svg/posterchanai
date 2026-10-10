"""Phase 2: chat history as encrypted, user-deletable Nostr events (docs/NOSTR_DATASTORE.md).

Each message is a kind-30078 doc `d=pcai:msg:<conv>:<seq>`, **NIP-44-encrypted to the user's
server-held storage key** and signed by it — so it's never on a timeline, only the user (server,
on their behalf) can read it, and it's individually deletable via NIP-09 (`delete_doc`). The
conversation LIST is conversation_table (an operator DocTable); the *messages* live here.

The relay is the only datastore (always on): routers/services call add/get/delete (chat_history).
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


async def get_messages(db, user, conv_id: int, *, strict: bool = False) -> list:
    """All messages for a conversation, oldest first, as [{role, content, ts}]. `strict` raises when
    the relay cannot answer instead of returning an empty transcript."""
    sk = user_storage_seckey(db, user)
    docs = await store.list_docs(_port(db), f"{store.NS_MSG}{conv_id}:", seckey=sk, strict=strict)
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


# ---- the conversation LIST is conversation_table (a relay DocTable, #161). The per-user `pcai:conv:<id>`
# documents written here before were its earlier mirror: nothing writes them any more, delete_conversation
# above still removes one when it exists, and `hydrate_conversations` reads them only until the
# conversations table's migration marker exists (it seeds SQL, and SQL is what that migration copies). ----
async def hydrate_conversations(db) -> int:
    """relay → conversations cache. Recreate missing Conversation rows for every user from their
    NS_CONV docs (so a fresh node restores the chat list). Additive only — never edits existing rows.
    Returns the number of conversations recreated. A no-op once the conversations table is relay-
    authoritative: SQL is not read after that, and filling it would only bring deleted chats back into
    the copy an older build reads."""
    from datetime import datetime
    from app.models import Conversation, User
    from app.services import conversation_table, table_gate
    if await table_gate.arelay_mode(conversation_table.TABLE):
        return 0
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


# The after_commit hook that mirrored every committed SQL `Message` row to the relay is gone with the
# rows themselves: since c3ed195a2 nothing inserts one (chat_history.append writes the encrypted
# event directly and awaits it), and the conversations a row would hang off are relay documents now.
