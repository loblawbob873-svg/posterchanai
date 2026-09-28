"""Texts ✨: draft a reply to the last message in a text-message conversation.

THE MODEL ONLY EVER PRODUCES TEXT THE USER THEN REVIEWS — the same rule as Mail's ✨ menu. The
draft lands in the composer, unsent and editable; nothing here sends a text.

WHAT CROSSES THE WIRE IS DELIBERATELY SMALL. SMS is some of the most private content on a phone, so
the client sends only the tail of the conversation — at most `MAX_CONTEXT` messages, each clipped —
labelled "me" or "them". No phone numbers, no contact names, no dates: a reply does not need them,
and what is never sent can never be logged, cached or leaked. Nothing here logs the text either
(sizes only — tests/test_no_prompt_logging.py).

THE ACCESS GATE IS THE AI GATE, not a new one: `nip05_access.ai_allowed` for an account with a
`User` row (admins, the admin-granted flag, a confirmed member), and `nip05_access.is_member` for a
key with no row — the phone signs its own request, and a member who never opened the web app has no
row, which is exactly the population the NIP-05 entitlement exists for.

THE MODEL CALL IS MAIL'S: `CommandService(db, user).chat_service.chat`, which already knows about
the load balancer, the proxy and the VRAM swap. Never a hosted API.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# The bounded window: the last message plus up to nine before it. Enough for "what are we talking
# about", small enough that a year-long thread never goes to the model.
MAX_CONTEXT = 10
MAX_CHARS_EACH = 1000
MAX_REPLY_CHARS = 1000


class TextsAiError(Exception):
    """A refusal with an HTTP status and a sentence a person can read."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def nostr_only() -> bool:
    return (os.getenv("POSTERCHANAI_NOSTR_ONLY", "0") or "").strip().lower() in ("1", "true", "yes", "on")


async def allowed(user, pubkey_hex: str = "") -> bool:
    """THE AI gate, via the one predicate. Never raises; anything unknown is False."""
    if nostr_only():
        return False
    from app.services import nip05_access
    try:
        if user is not None:
            return bool(await nip05_access.ai_allowed(user))
        if pubkey_hex:
            return bool(await nip05_access.is_member(pubkey_hex))
    except Exception as e:
        logger.debug("[texts-ai] access check failed: %s", type(e).__name__)
    return False


def clean_context(items) -> list:
    """[(mine: bool, text: str)] — the last MAX_CONTEXT non-empty messages, oldest first.

    Accepts dicts ({"me": bool, "text": str}) or objects with the same attributes. Blank messages
    are dropped rather than sent as empty turns; each text is clipped to MAX_CHARS_EACH."""
    out = []
    for it in list(items or []):
        if isinstance(it, dict):
            mine, text = it.get("me"), it.get("text")
        else:
            mine, text = getattr(it, "me", False), getattr(it, "text", "")
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_CHARS_EACH]
        if text:
            out.append((bool(mine), text))
    return out[-MAX_CONTEXT:]


def build_messages(context: list) -> list:
    """The chat messages for the model. The conversation is FENCED as quoted material and the ask
    comes last, next to an explicit cue — Mail learned that the local model, recency-biased,
    otherwise CONTINUES the quoted text instead of answering it."""
    if not context:
        raise TextsAiError(400, "There is no message to reply to yet.")
    context_lines = "\n".join(("Me: " if mine else "Them: ") + text for mine, text in context)
    last_mine = context[-1][0]
    ask = ("Write my next text in this conversation — a natural follow-up to my last message."
           if last_mine else
           "Write my reply to their last message.")
    return [
        {"role": "system", "content": (
            "You draft text-message (SMS) replies for the user, who is \"Me\" in the conversation. "
            "Write ONE short, casual text in the user's voice, the way people actually text: usually "
            "one or two sentences. Rules: never repeat or continue the other person's message; "
            "never invent facts, plans, times, places or promises that are not in the "
            "conversation — when a real answer needs something only the user knows, keep it "
            "open-ended or ask. Match the conversation's language and tone. Output ONLY the text "
            "of the reply: no quotes, no \"Me:\" label, no options, no commentary.")},
        {"role": "user", "content": "The conversation so far, oldest first:\n<<<TEXTS\n" + context_lines
            + "\nTEXTS\n\n" + ask + "\n\nMy text:"},
    ]


_LABEL = re.compile(r"^\s*(?:me|reply|my (?:text|reply))\s*:\s*", re.I)


def clean_draft(out: str) -> str:
    """Belt over the prompt's braces: strip a speaker label, wrapping quotes and a trailing
    commentary paragraph a local model sometimes adds, and bound the length."""
    s = (out or "").strip()
    s = _LABEL.sub("", s).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”":
        s = s[1:-1].strip()
    if len(s) >= 2 and s[0] == "“" and s[-1] == "”":
        s = s[1:-1].strip()
    # "Option 1: … Option 2: …" or a trailing "(Feel free to …)" — keep the first paragraph only
    # when the model produced several; a text is one message.
    parts = [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]
    if len(parts) > 1:
        s = parts[0]
    return s[:MAX_REPLY_CHARS].strip()


async def draft_reply(db, user, items) -> str:
    """Build the prompt from the bounded tail of the thread, ask the node's own model, return the
    cleaned draft. Raises TextsAiError with a readable sentence on every failure."""
    context = clean_context(items)
    msgs = build_messages(context)
    from app.services.command_service import CommandService
    cs = CommandService(db, user=user)
    # Task temperature, like Mail: a drafting tool, not a muse.
    try:
        cs.chat_service.temperature = 0.3
    except Exception:
        pass
    try:
        out = await cs.chat_service.chat(msgs) or ""
    except Exception as e:
        logger.warning("[texts-ai] model call failed (%d messages): %s", len(context), type(e).__name__)
        raise TextsAiError(502, "The AI did not answer — try again in a moment.")
    draft = clean_draft(out)
    if not draft:
        raise TextsAiError(502, "The AI did not come up with a reply — try again.")
    logger.info("[texts-ai] drafted a reply: %d context messages -> %d chars", len(context), len(draft))
    return draft
