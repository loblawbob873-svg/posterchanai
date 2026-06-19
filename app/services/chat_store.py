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


async def add_message(db, user, conv_id: int, role: str, content: str, ts: float | None = None) -> bool:
    """Append one chat message as an encrypted event. `seq` (ms + rand) keeps ordering + a unique d."""
    sk = user_storage_seckey(db, user)
    ts = ts if ts is not None else time.time()
    d = f"{store.NS_MSG}{conv_id}:{int(ts * 1000):015d}-{os.urandom(2).hex()}"
    return await store.put_doc(_port(db), sk, d,
                               {"conv": conv_id, "role": role, "content": content, "ts": ts})


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
