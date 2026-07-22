"""The conversation transcript, read and written as ENCRYPTED relay events only.

Chat messages used to be written to the Postgres `messages` table and mirrored to the relay
afterwards by an after_commit hook. That left a full PLAINTEXT copy of every conversation in SQL —
the relay copy was NIP-44 encrypted, but anyone with database access (or a backup/dump) could read
the lot in clear. Per-user counts confirmed it wasn't legacy residue: 34/34, 14/14, 12/12 …

It also created a lag. The mirror was fire-and-forget, so the relay trailed the synchronous SQL
commit by tens of seconds, and `get_conversation` had to compare counts and serve SQL rows whenever
the relay was behind — which is exactly when a client polls to recover a dropped live frame.

Writing the event DIRECTLY (awaited) removes both problems at once: there is no second copy to leak,
and no window in which the relay is behind, so no fallback is needed.

Every function is a thin wrapper over chat_store (which owns the encryption + d-tag layout). This
module exists so callers share ONE definition of "the transcript" instead of each reaching for
`conversation.messages`.
"""
import logging

logger = logging.getLogger(__name__)


async def append(db, user, conv_id: int, role: str, content: str,
                 image_path: str | None = None) -> bool:
    """Append one message. Awaited, so the transcript is readable the moment this returns."""
    from app.services import chat_store
    try:
        return await chat_store.add_message(db, user, conv_id, role, content or "",
                                            image_path=image_path)
    except Exception as e:
        # RETRY ONCE ON A FRESH SESSION. A long generation can hold the request's DB connection idle
        # past Postgres' idle_in_transaction timeout, and the key lookup below the write may need it
        # (only for legacy users without a keyfile entry) — that exact failure mode is why the old
        # SQL save had a retry. Losing the assistant's reply after a multi-minute render is the worst
        # possible outcome, so the retry survives the move off SQL.
        logger.warning("[chat-history] append failed (conv %s, %s): %s — retrying on a fresh session",
                       conv_id, role, e)
        try:
            from app.database import SessionLocal
            fresh = SessionLocal()
            try:
                return await chat_store.add_message(fresh, user, conv_id, role, content or "",
                                                    image_path=image_path)
            finally:
                fresh.close()
        except Exception as e2:
            # Never raise into the chat path: a failed transcript write must not break the response.
            logger.error("[chat-history] append FAILED for conv %s (%s): %s", conv_id, role, e2)
            return False


async def load(db, user, conv_id: int) -> list:
    """The whole transcript, oldest first: [{role, content, ts, image_path?}]."""
    from app.services import chat_store
    try:
        return await chat_store.get_messages(db, user, conv_id) or []
    except Exception as e:
        logger.warning("[chat-history] load failed (conv %s): %s", conv_id, e)
        return []


async def count(db, user, conv_id: int) -> int:
    return len(await load(db, user, conv_id))


def for_llm(msgs: list, current: str, limit: int = 20, clip: int = 500) -> list:
    """Turn a transcript into the model's `messages` list.

    Faithful to what the SQL path did: drop the in-flight turn, keep the last `limit`, collapse
    consecutive same-role turns, clip each to `clip` chars, and guarantee the last entry is the
    user's current message.
    """
    out, last_role = [], "system"
    for m in (msgs or [])[-(limit + 1):-1] if len(msgs or []) > 1 else []:
        role = (m.get("role") or "").strip()
        if not role or role == last_role:
            continue
        out.append({"role": role, "content": (m.get("content") or "")[:clip]})
        last_role = role
    if last_role != "user":
        out.append({"role": "user", "content": current})
    return out
