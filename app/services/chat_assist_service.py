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
ACTIONS = ("reply", "summarize", "links", "window", "window_event")

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


# ---- ✨ on a desktop window: answer IN the panel ------------------------------------------------------
# The window-manager ✨ (os.js toggleWindowAI) used to hand every question to the AI chat screen, which
# took the person away from the window they were asking about. It now answers in place; the context
# is what the panel already collects (a selection wins over the visible text, a native app is its
# title only) and nothing but the answer comes back -- the client offers Copy / Save to Notes /
# Insert, each one a thing the person clicks.
WINDOW_MAX = 4
WINDOW_EACH = 4000
WINDOW_TOTAL = 12000
INSTRUCTION_MAX = 1000


def window_context(windows) -> list:
    """[(title, kind, label, text)] -- at most WINDOW_MAX windows, each clipped, within the total."""
    out, total = [], 0
    for w in list(windows or [])[:WINDOW_MAX]:
        if not isinstance(w, dict):
            continue
        title = re.sub(r"\s+", " ", str(w.get("title") or "Window")).strip()[:160] or "Window"
        kind = "native app" if str(w.get("kind") or "") == "native app" else "PosterChan app"
        sel = re.sub(r"\s+", " ", str(w.get("selection") or "")).strip()
        text = sel or re.sub(r"\s+", " ", str(w.get("text") or "")).strip()
        text = text[:max(0, min(WINDOW_EACH, WINDOW_TOTAL - total))]
        total += len(text)
        out.append((title, kind, "selected text" if sel else "visible text", text))
    return out


def build_window_messages(context: list, instruction: str) -> list:
    instruction = re.sub(r"\s+", " ", str(instruction or "")).strip()[:INSTRUCTION_MAX]
    if not instruction:
        raise AssistError(400, "Ask something about the window first.")
    if not context:
        raise AssistError(400, "There is no window to ask about.")
    parts = []
    for i, (title, kind, label, text) in enumerate(context, 1):
        body = text if text else "(no text available -- only the window's name is known)"
        parts.append(f"Window {i}: \"{title}\" ({kind}), {label}:\n<<<WINDOW\n{body}\nWINDOW")
    return [
        {"role": "system", "content": (
            "You help the user with what is on their screen. Answer their request using ONLY the window "
            "content provided; if it does not contain what is needed, say so plainly instead of guessing. "
            "You cannot click, send, save or run anything: if they ask for a reply, a message or a "
            "command, write it out for them to use -- never claim it was done. Be concise; use short "
            "bullets starting with \"- \" for lists. Match the language of the content.")},
        {"role": "user", "content": "\n\n".join(parts) + "\n\nRequest: " + instruction},
    ]


async def ask_window(db, user, windows, instruction: str) -> str:
    context = window_context(windows)
    out = await _chat(db, user, build_window_messages(context, instruction), 0.3)
    if not out:
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    logger.info("[chat-assist] window answer: %d windows, %d chars in -> %d chars",
                len(context), sum(len(c[3]) for c in context), len(out))
    return out[:6000]


# ---- "Add to Calendar" from the window ✨ answer -------------------------------------------------------
# The model only PROPOSES: one event, as JSON, which the client opens in the Calendar's own New-event
# form for the person to correct and save (or cancel). Everything is validated here, so a malformed
# or invented field arrives as an empty one rather than as a wrong date on somebody's calendar.
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def build_event_messages(context: list, answer: str, today: str) -> list:
    if not context and not answer:
        raise AssistError(400, "There is nothing to find an event in.")
    today = today if _DATE.match(str(today or "")) else ""
    parts = [f"Window \"{t}\" ({k}), {lab}:\n<<<WINDOW\n{x or '(no text)'}\nWINDOW" for t, k, lab, x in context]
    if answer:
        parts.append("An earlier AI answer about it:\n<<<ANSWER\n" + str(answer)[:4000] + "\nANSWER")
    return [
        {"role": "system", "content": (
            "Find the ONE calendar event (an appointment, meeting, deadline, reservation) described in the "
            "text. Reply with ONLY a JSON object: {\"title\": short name, \"date\": \"YYYY-MM-DD\", "
            "\"start\": \"HH:MM\" 24-hour or \"\", \"end\": \"HH:MM\" or \"\", \"allDay\": true/false, "
            "\"location\": \"\" or the place, \"notes\": one short line}. Resolve relative dates "
            "(\"tomorrow\", \"next Friday\") against today's date. Use only what the text says -- never "
            "invent a time or a place. If there is no event with a date, reply {\"none\": true}.")},
        {"role": "user", "content": (f"Today is {today}.\n\n" if today else "") + "\n\n".join(parts)},
    ]


def parse_event(text: str):
    """The model's reply -> a clean event dict, or None when it found none (or said nothing usable)."""
    import json
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("none"):
        return None
    clean = lambda v, n: re.sub(r"\s+", " ", str(v or "")).strip()[:n]
    date = clean(raw.get("date"), 10)
    title = clean(raw.get("title"), 120)
    if not _DATE.match(date) or not title:
        return None
    start, end = clean(raw.get("start"), 5), clean(raw.get("end"), 5)
    start = start if _TIME.match(start) else ""
    end = end if (start and _TIME.match(end) and end > start) else ""
    return {"title": title, "date": date, "start": start, "end": end,
            "allDay": not start, "location": clean(raw.get("location"), 200),
            "notes": clean(raw.get("notes"), 500)}


async def window_event(db, user, windows, answer: str, today: str):
    context = window_context(windows)
    out = await _chat(db, user, build_event_messages(context, answer, today), 0.1)
    ev = parse_event(out)
    if ev is None:
        raise AssistError(422, "No event with a date was found there.")
    logger.info("[chat-assist] window event: %d windows -> event found", len(context))
    return ev
