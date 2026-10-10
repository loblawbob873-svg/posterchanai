"""Chat conversations (the old `conversations` table) as a DocTable on this node's relay (#161, wave 2).

One row per conversation: document `pcai:t:conversations:<id>`, row `{"user_id", "title", "created_at",
"updated_at"}` (naive-UTC ISO strings), NIP-44-encrypted to the operator key. The whole list is small (a few
per user), so every process holds it in memory like any DocTable.

The MESSAGES are not here and never were in this table's move: since c3ed195a2 (2026-07-22) a transcript is one
encrypted document per message under the USER's storage key (`pcai:msg:<conversation_id>:<seq>`, chat_store /
chat_history), read per conversation on demand. That is what a conversation id names on the relay, so ids are
kept exactly: a migrated row keeps its SQL id, and a new one gets `app_tables.new_id()` (time-based, unique across
processes, far above every SQL id, inside a JavaScript number).

Relay-only: there is no SQL side (a node's old rows are copied once, ids kept, by scripts/migrate_tables_to_nostr.py).

Callers get `Conv` objects with the old row's attributes (`id`, `user_id`, `title`, `created_at`, `updated_at`), so
`ConversationResponse.model_validate(conv)` and `conv.title = ...; await asave(db, conv)` read as before.
"""
import logging
from datetime import datetime

from app.services import app_tables, table_gate
from app.services.doc_table import DocTable

logger = logging.getLogger(__name__)

TABLE = "conversations"
DEFAULT_TITLE = "New Chat"


def _t() -> DocTable:
    return DocTable(TABLE)


class Conv:
    __slots__ = ("id", "user_id", "title", "created_at", "updated_at")

    def __init__(self, id, user_id, title=DEFAULT_TITLE, created_at=None, updated_at=None):  # noqa: A002
        self.id = int(id)
        self.user_id = int(user_id)
        self.title = title if title is not None else DEFAULT_TITLE
        now = datetime.utcnow()
        self.created_at = app_tables.from_iso(created_at) or now
        self.updated_at = app_tables.from_iso(updated_at) or self.created_at

    def row(self) -> dict:
        return {"user_id": self.user_id, "title": self.title,
                "created_at": app_tables.to_iso(self.created_at), "updated_at": app_tables.to_iso(self.updated_at)}

    @classmethod
    def from_row(cls, k, r: dict) -> "Conv":
        return cls(int(k), r.get("user_id"), r.get("title"), r.get("created_at"), r.get("updated_at"))

    def __repr__(self):
        return "Conv(id=%s, user_id=%s)" % (self.id, self.user_id)


# ------------------------------------------------------------------ reads
def _own(conv, user_id):
    if conv is None:
        return None
    if user_id is not None and conv.user_id != int(user_id):
        return None
    return conv


async def aget(db, conv_id, user_id=None):
    """The conversation, or None (no such id, or -- with `user_id` -- somebody else's). Unavailable when the relay
    could not be asked."""
    try:
        conv_id = int(conv_id)
    except (TypeError, ValueError):
        return None
    r = await table_gate.aget_row(_t(), str(conv_id))
    return _own(Conv.from_row(conv_id, r) if r is not None else None, user_id)


def get(db, conv_id, user_id=None):
    try:
        conv_id = int(conv_id)
    except (TypeError, ValueError):
        return None
    r = table_gate.get_row(_t(), str(conv_id))
    return _own(Conv.from_row(conv_id, r) if r is not None else None, user_id)


def _sorted(convs) -> list:
    return sorted(convs, key=lambda c: (c.updated_at, c.id), reverse=True)


async def alist_for_user(db, user_id) -> list:
    """The user's conversations, most recently updated first."""
    uid = int(user_id)
    return _sorted(Conv.from_row(k, r) for k, r in await table_gate.arows(_t()) if r.get("user_id") == uid)


def list_for_user(db, user_id) -> list:
    uid = int(user_id)
    return _sorted(Conv.from_row(k, r) for k, r in table_gate.rows(_t()) if r.get("user_id") == uid)


async def afind_by_title(db, user_id, title: str):
    """The user's most recently updated conversation with exactly this title (the Telegram / Logs / Reminders
    conversations are found this way), or None."""
    for c in await alist_for_user(db, user_id):
        if c.title == title:
            return c
    return None


def find_by_title(db, user_id, title: str):
    for c in list_for_user(db, user_id):
        if c.title == title:
            return c
    return None


# ------------------------------------------------------------------ writes
async def acreate(db, user_id, title: str = DEFAULT_TITLE) -> Conv:
    now = datetime.utcnow()
    title = title or DEFAULT_TITLE
    t = _t()
    held = {k for k, _r in t.rows_view()} if t.loaded else ()
    conv = Conv(app_tables.new_id(held), user_id, title, now, now)
    await t.aput(str(conv.id), conv.row())
    return conv


def create(db, user_id, title: str = DEFAULT_TITLE) -> Conv:
    now = datetime.utcnow()
    title = title or DEFAULT_TITLE
    t = _t()
    held = {k for k, _r in t.rows_view()} if t.loaded else ()
    conv = Conv(app_tables.new_id(held), user_id, title, now, now)
    t.put(str(conv.id), conv.row())
    return conv


async def aget_or_create(db, user_id, title: str) -> Conv:
    """The user's conversation with this title, made when there is none (Telegram, Logs, Reminders)."""
    conv = await afind_by_title(db, user_id, title)
    return conv if conv is not None else await acreate(db, user_id, title)


def get_or_create(db, user_id, title: str) -> Conv:
    conv = find_by_title(db, user_id, title)
    return conv if conv is not None else create(db, user_id, title)


async def asave(db, conv: Conv) -> None:
    """Store a changed title / updated_at."""
    await _t().aput(str(conv.id), conv.row())


def save(db, conv: Conv) -> None:
    _t().put(str(conv.id), conv.row())


async def atouch(db, conv: Conv, title: str | None = None) -> None:
    """updated_at = now (and a new title when given), stored."""
    if title is not None:
        conv.title = title
    conv.updated_at = datetime.utcnow()
    await asave(db, conv)


async def adelete(db, conv_ids) -> None:
    """Remove conversations (their transcripts are the caller's: chat_store.delete_conversation)."""
    ids = [int(conv_ids)] if isinstance(conv_ids, (int, str)) else [int(i) for i in conv_ids]
    for i in ids:
        await _t().adelete(str(i))


def delete(db, conv_ids) -> None:
    ids = [int(conv_ids)] if isinstance(conv_ids, (int, str)) else [int(i) for i in conv_ids]
    for i in ids:
        _t().delete(str(i))


async def apurge_user(db, user_id) -> list:
    """Every conversation of a deleted account (the old FK cascade). Returns the ids removed."""
    ids = [c.id for c in await alist_for_user(db, user_id)]
    await adelete(db, ids)
    return ids


def purge_user(db, user_id) -> list:
    ids = [c.id for c in list_for_user(db, user_id)]
    delete(db, ids)
    return ids


async def aensure(db, conv_id, user_id, title: str) -> Conv:
    """The conversation `conv_id`, recreated under that SAME id when it is gone (a background job outlived
    the chat it was started from: its result is delivered to that id, which the client is still listening
    on), with updated_at = now. Unavailable when the relay could not be asked -- never "gone"."""
    conv = await aget(db, conv_id)
    if conv is None:
        conv = Conv(int(conv_id), user_id, title)
    await atouch(db, conv)
    return conv
