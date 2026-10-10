"""Chat conversations (the old `conversations` table) as a DocTable on this node's relay (#161, wave 2).

One row per conversation: document `pcai:t:conversations:<id>`, row `{"user_id", "title", "created_at",
"updated_at"}` (naive-UTC ISO strings), NIP-44-encrypted to the operator key. The whole list is small (a few
per user), so every process holds it in memory like any DocTable.

The MESSAGES are not here and never were in this table's move: since c3ed195a2 (2026-07-22) a transcript is one
encrypted document per message under the USER's storage key (`pcai:msg:<conversation_id>:<seq>`, chat_store /
chat_history), read per conversation on demand. That is what a conversation id names on the relay, so ids are
kept exactly: a migrated row keeps its SQL id, and a new one gets `app_tables.new_id()` (time-based, unique across
processes, far above every SQL id, inside a JavaScript number).

Until the `conversations` migration marker exists SQL is authoritative (table_gate): a new conversation takes its
id from the SQL sequence (the column is a 32-bit integer -- a time-based id does not fit), its relay document is
written before the SQL commit, and every change goes to both. After the marker only the relay is used.

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

    @classmethod
    def from_sql(cls, c) -> "Conv":
        return cls(c.id, c.user_id, c.title, c.created_at, c.updated_at)

    def __repr__(self):
        return "Conv(id=%s, user_id=%s)" % (self.id, self.user_id)


def sql_rows(db) -> dict:
    """{id: row} for the whole SQL table -- the migration's source."""
    from app.models import Conversation
    return {str(c.id): Conv.from_sql(c).row() for c in db.query(Conversation).all()}


# ------------------------------------------------------------------ SQL side
def _sql_get(db, conv_id):
    from app.models import Conversation
    return db.query(Conversation).filter(Conversation.id == int(conv_id)).first()


def _sql_list(db, user_id) -> list:
    from app.models import Conversation
    return [Conv.from_sql(c) for c in db.query(Conversation).filter(Conversation.user_id == int(user_id)).all()]


def _sql_save(db, conv: Conv) -> None:
    from app.models import Conversation
    c = _sql_get(db, conv.id)
    if c is None:
        c = Conversation(id=conv.id, user_id=conv.user_id)
        db.add(c)
    c.user_id, c.title, c.created_at, c.updated_at = conv.user_id, conv.title, conv.created_at, conv.updated_at
    db.commit()


def _sql_reserve(db, user_id, title, now) -> int:
    """A new SQL row, FLUSHED but not committed: the sequence gives its id, and it is committed only after the
    relay holds the document (`_sql_commit`) -- or rolled back when the relay refused it."""
    from app.models import Conversation
    c = Conversation(user_id=int(user_id), title=title, created_at=now, updated_at=now)
    db.add(c)
    db.flush()
    return int(c.id)


def _sql_delete(db, conv_ids) -> None:
    from app.models import Conversation, Message
    ids = [int(i) for i in conv_ids]
    if not ids:
        return
    # the legacy plaintext transcript rows go with their conversation, as the FK cascade always did
    db.query(Message).filter(Message.conversation_id.in_(ids)).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.id.in_(ids)).delete(synchronize_session=False)
    db.commit()


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
    if not await table_gate.arelay_mode(TABLE):
        c = _sql_get(db, conv_id)
        return _own(Conv.from_sql(c) if c is not None else None, user_id)
    r = await table_gate.aget_row(_t(), str(conv_id))
    return _own(Conv.from_row(conv_id, r) if r is not None else None, user_id)


def get(db, conv_id, user_id=None):
    try:
        conv_id = int(conv_id)
    except (TypeError, ValueError):
        return None
    if not table_gate.relay_mode(TABLE):
        c = _sql_get(db, conv_id)
        return _own(Conv.from_sql(c) if c is not None else None, user_id)
    r = table_gate.get_row(_t(), str(conv_id))
    return _own(Conv.from_row(conv_id, r) if r is not None else None, user_id)


def _sorted(convs) -> list:
    return sorted(convs, key=lambda c: (c.updated_at, c.id), reverse=True)


async def alist_for_user(db, user_id) -> list:
    """The user's conversations, most recently updated first."""
    if not await table_gate.arelay_mode(TABLE):
        return _sorted(_sql_list(db, user_id))
    uid = int(user_id)
    return _sorted(Conv.from_row(k, r) for k, r in await table_gate.arows(_t()) if r.get("user_id") == uid)


def list_for_user(db, user_id) -> list:
    if not table_gate.relay_mode(TABLE):
        return _sorted(_sql_list(db, user_id))
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
    if await table_gate.arelay_mode(TABLE):
        t = _t()
        held = {k for k, _r in t.rows_view()} if t.loaded else ()
        conv = Conv(app_tables.new_id(held), user_id, title, now, now)
        await t.aput(str(conv.id), conv.row())
        return conv
    try:
        conv = Conv(_sql_reserve(db, user_id, title, now), user_id, title, now, now)
        await _t().aput(str(conv.id), conv.row())
        db.commit()
    except Exception:
        db.rollback()
        raise
    return conv


def create(db, user_id, title: str = DEFAULT_TITLE) -> Conv:
    now = datetime.utcnow()
    title = title or DEFAULT_TITLE
    if table_gate.relay_mode(TABLE):
        t = _t()
        held = {k for k, _r in t.rows_view()} if t.loaded else ()
        conv = Conv(app_tables.new_id(held), user_id, title, now, now)
        t.put(str(conv.id), conv.row())
        return conv
    try:
        conv = Conv(_sql_reserve(db, user_id, title, now), user_id, title, now, now)
        _t().put(str(conv.id), conv.row())
        db.commit()
    except Exception:
        db.rollback()
        raise
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
    sql = not await table_gate.arelay_mode(TABLE)
    await _t().aput(str(conv.id), conv.row())
    if sql:
        _sql_save(db, conv)


def save(db, conv: Conv) -> None:
    sql = not table_gate.relay_mode(TABLE)
    _t().put(str(conv.id), conv.row())
    if sql:
        _sql_save(db, conv)


async def atouch(db, conv: Conv, title: str | None = None) -> None:
    """updated_at = now (and a new title when given), stored."""
    if title is not None:
        conv.title = title
    conv.updated_at = datetime.utcnow()
    await asave(db, conv)


async def adelete(db, conv_ids) -> None:
    """Remove conversations (their transcripts are the caller's: chat_store.delete_conversation)."""
    ids = [int(conv_ids)] if isinstance(conv_ids, (int, str)) else [int(i) for i in conv_ids]
    sql = not await table_gate.arelay_mode(TABLE)
    for i in ids:
        await _t().adelete(str(i))
    if sql:
        _sql_delete(db, ids)


def delete(db, conv_ids) -> None:
    ids = [int(conv_ids)] if isinstance(conv_ids, (int, str)) else [int(i) for i in conv_ids]
    sql = not table_gate.relay_mode(TABLE)
    for i in ids:
        _t().delete(str(i))
    if sql:
        _sql_delete(db, ids)


async def apurge_user(db, user_id) -> list:
    """Every conversation of a deleted account (the old FK cascade). Returns the ids removed."""
    ids = [c.id for c in await alist_for_user(db, user_id)]
    if not await table_gate.arelay_mode(TABLE):
        # relay rows the SQL list may not show (a document whose SQL commit failed) go too
        uid = int(user_id)
        ids = sorted(set(ids) | {int(k) for k, r in await table_gate.arows(_t()) if r.get("user_id") == uid})
    await adelete(db, ids)
    return ids


def purge_user(db, user_id) -> list:
    ids = [c.id for c in list_for_user(db, user_id)]
    if not table_gate.relay_mode(TABLE):
        uid = int(user_id)
        ids = sorted(set(ids) | {int(k) for k, r in table_gate.rows(_t()) if r.get("user_id") == uid})
    delete(db, ids)
    return ids


def delete_legacy_sql_rows(db, user_id) -> None:
    """A deleted account's SQL conversation + legacy transcript rows, in the caller's transaction (no commit).
    Needed in EITHER mode: the users row cannot go while rows still reference it (databases created before
    the FK had ON DELETE CASCADE), and the legacy rows hold plaintext transcripts."""
    from app.models import Conversation, Message
    ids = [c.id for c in db.query(Conversation.id).filter(Conversation.user_id == int(user_id)).all()]
    if ids:
        db.query(Message).filter(Message.conversation_id.in_(ids)).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.user_id == int(user_id)).delete(synchronize_session=False)


async def aensure(db, conv_id, user_id, title: str) -> Conv:
    """The conversation `conv_id`, recreated under that SAME id when it is gone (a background job outlived
    the chat it was started from: its result is delivered to that id, which the client is still listening
    on), with updated_at = now. Unavailable when the relay could not be asked -- never "gone"."""
    conv = await aget(db, conv_id)
    if conv is None:
        conv = Conv(int(conv_id), user_id, title)
    await atouch(db, conv)
    return conv
