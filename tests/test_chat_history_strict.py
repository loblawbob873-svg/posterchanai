"""AI chat: /api/conversations/<id> answers 503 when the relay cannot be read, never an empty
transcript (which the AI screen drew as a brand-new chat during every relay restart)."""
import asyncio
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.routers import chat as R
from app.services import chat_store, nostr_store


def test_the_history_read_is_strict_and_a_failure_is_a_503():
    seen = {}

    async def down(db, user, conv_id, **kw):
        seen.update(kw)
        raise ConnectionError("relay not listening")
    db = mock.MagicMock()
    from app.services import conversation_table

    async def conv(db, conv_id, user_id=None):      # the conversation row exists (a relay DocTable since #161)
        return SimpleNamespace(id=5, user_id=1, title="Trip", created_at=None, updated_at=None)
    with mock.patch.object(conversation_table, "aget", conv), \
         mock.patch.object(chat_store, "enabled", lambda db: True), \
         mock.patch.object(chat_store, "get_messages", down):
        try:
            asyncio.run(R.get_conversation(5, db=db, current_user=SimpleNamespace(id=1, username="u")))
            code = 200
        except HTTPException as e:
            code = e.status_code
    assert code == 503 and seen.get("strict") is True


def test_get_messages_passes_strict_to_the_relay():
    got = {}

    async def fake(port, prefix, **kw):
        got.update(kw)
        return {}
    with mock.patch.object(nostr_store, "list_docs", fake), \
         mock.patch.object(chat_store, "user_storage_seckey", lambda db, u: b"k" * 32), \
         mock.patch.object(chat_store, "_port", lambda db: 3052):
        asyncio.run(chat_store.get_messages(None, None, 5, strict=True))
    assert got.get("strict") is True
