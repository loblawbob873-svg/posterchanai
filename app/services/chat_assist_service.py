"""✨ in Telegram and Direct Messages: draft a reply, summarize the chat, summarize its links.

Asked for: "AI replies to Telegram and DM's also" and "For Telegram, maybe the AI sparkle button should
have a Generate Reply Summarize youtube and links". Three actions on the same conversation:

  * reply      -- texts_ai_service's drafting, told what it is drafting for (a Telegram message, a
                  direct message). Several different drafts in ONE model call; the person picks one
                  and it lands in the composer UNSENT. Same never-invent rules as Texts.
  * summarize  -- what the chat has been about, decisions, and anything waiting on the user.
  * links      -- the links in the recent messages, each read through the SAME SSRF-guarded fetcher
                  the rest of the app uses (search_service.fetch_url_content, which also turns a
                  YouTube link into its transcript) and summarized.

The model only ever produces text the person reads. Nothing here sends a message, and nothing logs
what anybody wrote (sizes only -- tests/test_no_prompt_logging.py). The model call is the node's own
(CommandService's chat service: load balancer, proxy, VRAM swap), never a hosted API. The gate is the
AI gate (texts_ai_service.allowed -> nip05_access).
"""
from __future__ import annotations

import logging
import re

from app.services.texts_ai_service import TextsAiError as AssistError

logger = logging.getLogger(__name__)

MEDIA = {"telegram": "Telegram chat message", "dm": "direct message (DM)"}
ACTIONS = ("reply", "summarize", "links")

# A summary may read further back than a reply needs, but a whole year of a group chat never goes to
# the model: the newest SUMMARY_MSGS messages, each clipped, and a total ceiling on top.
SUMMARY_MSGS = 80
SUMMARY_EACH = 600
SUMMARY_TOTAL = 24000
MAX_LINKS = 3
LINK_CHARS = 12000
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)


def medium_of(name: str) -> str:
    return MEDIA.get(str(name or "").lower(), MEDIA["dm"])


def summary_context(items) -> list:
    """[(mine, who, text)] -- the newest SUMMARY_MSGS non-empty messages, oldest first, within the
    total ceiling (the OLDEST are the ones dropped)."""
    rows = []
    for it in list(items or []):
        if not isinstance(it, dict):
            continue
        text = re.sub(r"\s+", " ", str(it.get("text") or "")).strip()[:SUMMARY_EACH]
        if not text:
            continue
        who = re.sub(r"\s+", " ", str(it.get("who") or "")).strip()[:40]
        rows.append((bool(it.get("me")), who, text))
    rows = rows[-SUMMARY_MSGS:]
    total, kept = 0, []
    for row in reversed(rows):
        total += len(row[2]) + 8
        if total > SUMMARY_TOTAL:
            break
        kept.append(row)
    return list(reversed(kept))


def build_summary_messages(context: list, medium: str) -> list:
    if not context:
        raise AssistError(400, "There is nothing in this chat to summarize yet.")
    lines = "\n".join(("Me" if mine else (who or "Them")) + ": " + text for mine, who, text in context)
    return [
        {"role": "system", "content": (
            f"You summarize a {medium} conversation for the user, who is \"Me\". Write 3-7 short bullet "
            "points, each starting with \"- \": what has been discussed, anything decided, and anything "
            "that is waiting on the user (a question to answer, something they said they would do). "
            "Only what the messages say -- never invent names, times, plans or facts. Match the "
            "conversation's language. Output only the bullets.")},
        {"role": "user", "content": "The conversation, oldest first:\n<<<CHAT\n" + lines + "\nCHAT\n\nSummary:"},
    ]


def links_in(items, limit: int = MAX_LINKS) -> list:
    """The distinct http(s) links in the messages, NEWEST first (the ones the person is most likely
    asking about), trailing punctuation trimmed."""
    out = []
    for it in reversed(list(items or [])):
        text = str((it or {}).get("text") or "") if isinstance(it, dict) else ""
        for url in _URL.findall(text):
            url = url.rstrip(".,;:!?)]}'\"")
            if url not in out:
                out.append(url)
            if len(out) >= limit:
                return out
    return out


async def _chat(db, user, msgs, temperature: float) -> str:
    from app.services.command_service import CommandService
    cs = CommandService(db, user=user)
    try:
        cs.chat_service.temperature = temperature
    except Exception:
        pass
    try:
        return (await cs.chat_service.chat(msgs) or "").strip()
    except Exception as e:
        logger.warning("[chat-assist] model call failed: %s", type(e).__name__)
        raise AssistError(502, "The AI did not answer — try again in a moment.")


async def summarize(db, user, items, medium: str) -> str:
    context = summary_context(items)
    out = await _chat(db, user, build_summary_messages(context, medium), 0.2)
    if not out:
        raise AssistError(502, "The AI did not come up with a summary — try again.")
    logger.info("[chat-assist] summarized %d messages -> %d chars", len(context), len(out))
    return out[:4000]


async def summarize_links(db, user, items) -> list:
    urls = links_in(items)
    if not urls:
        raise AssistError(400, "There are no links in the recent messages.")
    from app.services.search_service import get_search_service
    svc = get_search_service(db)
    results = []
    for url in urls:
        try:
            page = await svc.fetch_url_content(url, max_length=LINK_CHARS)
        except Exception as e:
            page = {"error": type(e).__name__}
        text = ((page or {}).get("content") or "").strip()
        title = (page or {}).get("title") or url
        if not page or page.get("error") or len(text) < 80:
            results.append({"url": url, "title": title,
                            "error": (page or {}).get("error") or "Nothing readable at that link."})
            continue
        summary = await _chat(db, user, [
            {"role": "system", "content": (
                "Summarize the page or video transcript the user provides in 3-5 sentences of plain "
                "prose. Lead with what it actually says. No preamble, no title, no commentary about the "
                "summary. If the text is mostly navigation or boilerplate, say so instead of inventing.")},
            {"role": "user", "content": f"Title: {title}\nURL: {url}\n\n{text}"}], 0.2)
        results.append({"url": url, "title": title, "summary": summary[:2000]})
    logger.info("[chat-assist] summarized %d link(s)", len(results))
    return results
