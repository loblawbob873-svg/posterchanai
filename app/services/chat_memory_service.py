"""Rolling chat memory: a short digest of the turns that have fallen out of the model's window.

The chat sends the last ~20 messages and nothing else, so a long conversation forgets its own
beginning — your name, what you're working on, decisions taken 40 messages ago. The obvious fix,
sending more history, is the wrong one on a node with a 16k context shared with everything else:
it makes every request slower and eventually truncates anyway.

So instead: once messages age out of the window, fold them into ~200 tokens ONCE, and carry that
tiny digest along forever. The result is a chat that remembers while each request stays SMALL —
the rare feature that makes weak hardware faster rather than slower. Cost is one short generation
per ~10 aged-out messages, and it runs AFTER the reply, so nobody ever waits on it.
"""
import asyncio
import logging

from app.database import SessionLocal
from app.models import Conversation, Message

logger = logging.getLogger(__name__)

# Must match the history slice in app/routers/chat.py ([-21:-1] = the last 20 turns). Messages older
# than this are the ones the model can no longer see, and therefore the ones worth summarising.
HISTORY_TURNS = 20
MIN_NEW = 8            # don't burn a generation to fold in one or two stale turns
MAX_SRC_CHARS = 6000   # cap the input: this shares a GPU with image/music/video and the bots

_SYS = (
    "You maintain a running memory of a conversation for an assistant with a short context window. "
    "Given the previous memory (if any) and the messages that have just scrolled out of view, write "
    "the UPDATED memory: the durable facts worth carrying — who the user is, what they are working "
    "on, decisions and preferences they stated, unresolved threads. Keep names, numbers, paths and "
    "identifiers exactly. Drop small talk and anything already resolved. Write compact prose or short "
    "dashes, under 180 words, no headings and no preamble — output only the memory itself."
)


def summary_for(conversation) -> str | None:
    """The digest to prepend to a request, or None. Read-only and free — safe in the hot path."""
    try:
        s = (conversation.summary or "").strip()
        return s or None
    except Exception:
        return None


def _pending(db, conversation_id: int):
    """(messages that have aged out and aren't yet summarised, highest id among ALL aged-out ones)."""
    msgs = (db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.id).all())
    if len(msgs) <= HISTORY_TURNS:
        return [], 0
    aged = msgs[:-HISTORY_TURNS]
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    upto = (conv.summary_upto_id or 0) if conv else 0
    return [m for m in aged if m.id > upto], aged[-1].id


async def refresh(conversation_id: int, user_id: int | None = None) -> bool:
    """Fold newly-aged-out messages into the conversation's memory. Opens its OWN session — this runs
    after the request that triggered it has finished, and reusing that closed session is the
    documented way to lose the write. Never raises: memory is an enhancement, not the chat."""
    db = SessionLocal()
    try:
        new_msgs, upto = _pending(db, conversation_id)
        if len(new_msgs) < MIN_NEW:
            return False
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if not conv:
            return False
        prev = (conv.summary or "").strip()

        parts = []
        if prev:
            parts.append(f"Previous memory:\n{prev}\n")
        parts.append("Messages that just scrolled out of view:")
        for m in new_msgs:
            who = "User" if m.role == "user" else "Assistant"
            body = (m.content or "").strip().replace("\n", " ")
            if body:
                parts.append(f"{who}: {body[:400]}")
        src = "\n".join(parts)[:MAX_SRC_CHARS]

        # ChatService, NOT get_inference_service: the latter hands back the LOCAL llama service, so
        # the summary would always burn the local GPU even when a peer node is idle. ChatService
        # goes through peer offload and the site load balancer first, like a normal chat turn — this
        # is background work and has no business jumping the queue on a GPU that is already shared
        # with image, music, video and the bot fleet.
        from app.services.chat_service import ChatService
        out = (await ChatService(db, user=None).chat(
            [{"role": "system", "content": _SYS}, {"role": "user", "content": src}]) or "").strip()
        if not out or out.lower().startswith("error:"):
            return False

        conv.summary = out[:4000]
        conv.summary_upto_id = upto
        db.commit()
        logger.info("[chat-memory] conversation %s: folded %d messages (now %d chars)",
                    conversation_id, len(new_msgs), len(conv.summary))
        return True
    except Exception as e:
        logger.warning("[chat-memory] refresh failed for conversation %s: %s", conversation_id, e)
        return False
    finally:
        db.close()


# Tasks are kept in a set for the whole of their life. asyncio only holds a WEAK reference to a
# running task, so a fire-and-forget create_task() can be garbage-collected mid-flight — the summary
# would vanish silently, occasionally, under load, which is the worst kind of bug to chase.
_tasks: set = set()
# One refresh per conversation at a time. Two quick turns would otherwise both see the same aged-out
# messages and each burn a generation on them; the second write also clobbers the first.
_inflight: set = set()


async def _guarded(conversation_id: int) -> bool:
    if conversation_id in _inflight:
        return False
    _inflight.add(conversation_id)
    try:
        return await refresh(conversation_id)
    finally:
        _inflight.discard(conversation_id)


def schedule_refresh(conversation_id: int) -> None:
    """Fire-and-forget the refresh so the user never waits on it. No-op without a running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_guarded(conversation_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
