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
ACTIONS = ("reply", "summarize", "links", "window", "window_event", "window_steps")

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


# ---- The window ✨ panel, INTERACTIVE: an answer, a task list and steps the person runs with buttons ----
# "we need interactive Agentic features with buttons, not loading up AI Chat" / "Extract Tasks need to be
# functional and actually useful". The model PROPOSES; the panel turns each proposal into a button, and
# nothing happens until the person presses it. Everything is validated here, so a malformed or invented
# step arrives as no step rather than as a wrong command in somebody's terminal.
STEP_KINDS = ("command", "insert", "note", "calendar", "open", "search")
# OPERATING THE WINDOW ITSELF ("need way to interact with the current window and do stuff"): the client
# sends the window's visible controls, numbered; a step names one of THOSE numbers and nothing else, so
# the model can only press what is really on screen. The client asks before anything that sends/deletes.
ACT_KINDS = ("click", "fill", "choose", "toggle", "press", "scroll")
# Keys a "press" may send. Navigation and submit only -- nothing that edits text by itself.
PRESS_KEYS = ("Enter", "Escape", "Tab", "ArrowDown", "ArrowUp")
CONTROL_MAX = 80
STEP_MAX = 4
ACT_STEP_MAX = 8
TASK_MAX = 20
OPEN_VIEWS = {"notes": "Notes", "calendar": "Calendar", "files": "Files", "terminal": "Terminal",
              "mail": "Email", "websearch": "Web Search", "texts": "Texts", "contacts": "Contacts",
              "messages": "Messages", "tg": "Telegram"}
HISTORY_MAX = 4


def clean_controls(controls) -> list:
    """The window's controls as the client numbered them -> [(ref, role, label, value, near)], validated.
    `near` is the heading the control sits under, or the start of the post/row it belongs to -- what tells
    two "Save" buttons, or every post's "reply", apart."""
    out = []
    for c in list(controls or [])[:CONTROL_MAX]:
        if not isinstance(c, dict):
            continue
        try:
            ref = int(c.get("ref"))
        except (TypeError, ValueError):
            continue
        role = _clean(c.get("role"), 20).lower()
        label = _clean(c.get("label"), 80)
        if ref <= 0 or not role or not label:
            continue
        out.append((ref, role, label, _clean(c.get("value"), 80), _clean(c.get("near"), 90)))
    return out


def build_steps_messages(context: list, instruction: str, history=None, commands: bool = False,
                         today: str = "", controls=None) -> list:
    base = build_window_messages(context, instruction)          # validates + fences the windows
    today = today if _DATE.match(str(today or "")) else ""
    ctl = clean_controls(controls)
    kinds = ["\"insert\": text to put into the window's own text box (a reply, a rewrite)",
             "\"note\": text worth keeping, saved to the user's Notes",
             "\"calendar\": one event with a date, added to the Calendar after the user checks it",
             "\"open\": open an app -- text is one of " + ", ".join(OPEN_VIEWS),
             "\"search\": a web search -- text is the query"]
    if ctl:
        kinds = ["\"click\": press control number \"ref\" (a button, link, tab)",
                 "\"fill\": type \"text\" into text box number \"ref\" (replaces what is there)",
                 "\"choose\": pick the option labelled \"text\" in list number \"ref\"",
                 "\"toggle\": set checkbox number \"ref\" to \"on\": true or false",
                 "\"press\": press a key on control number \"ref\" -- text is one of " + ", ".join(PRESS_KEYS)
                 + " (Enter submits a search box or form, Escape closes a menu or dialog)",
                 "\"scroll\": scroll the window to see more -- text is \"down\" or \"up\"; after it, the user "
                 "presses Continue and you see what came into view"] + kinds
    if commands:
        kinds.insert(0, "\"command\": ONE shell command for this terminal, one line; prefer read-only "
                        "commands that show what is going on; never anything destructive unless asked")
    system = (
        "You are PosterChan's on-screen assistant. You act ONLY through buttons the user presses, so you "
        "never claim anything was done. Reply with ONE JSON object and nothing else:\n"
        "{\"answer\": short, clear text -- plain sentences and \"- \" bullets, \n"
        " \"tasks\": [{\"text\": one concrete action starting with a verb, \"due\": \"YYYY-MM-DD\" or \"\", "
        "\"who\": the person responsible if named, else \"\"}],\n"
        " \"steps\": [{\"do\": kind, \"label\": 2-5 word button text, \"text\": the content"
        + (", \"ref\": control number, \"on\": true/false" if ctl else "") + "}]}\n"
        "Step kinds: " + "; ".join(kinds) + ".\n"
        + ("When the user asks you to DO something in this window, do it with its controls: the steps, in "
           "order, that a person would take (fill the fields, then press the button). Use ONLY the numbered "
           "controls listed; never invent one. If a step needs something you cannot know, ask in \"answer\" "
           "instead of guessing. Anything you would TELL the user to click or type goes in \"steps\" -- never only in "
           "\"answer\". Example: {\"answer\": \"Searching your notes for bugs.\", \"tasks\": [], \"steps\": "
           "[{\"do\": \"fill\", \"ref\": 2, \"text\": \"bugs\", \"label\": \"Search bugs\"}, "
           "{\"do\": \"press\", \"ref\": 2, \"text\": \"Enter\", \"label\": \"Run search\"}]}. Never fill in a "
           "placeholder such as [your title]; if you do not know the text, ask. " if ctl else "")
        + f"At most {ACT_STEP_MAX if ctl else STEP_MAX} steps, only ones that genuinely help with the request; [] when none do. "
        "Fill \"tasks\" when the request is about tasks, action items, follow-ups or deadlines -- every real "
        "commitment, decision to act or deadline in the content, one per item, nothing invented; otherwise []. "
        "Resolve relative dates (\"Friday\", \"tomorrow\") against today's date. Use ONLY the window content; "
        "if it does not contain what is needed, say so in \"answer\". Match the content's language.")
    hist = []
    for h in list(history or [])[-HISTORY_MAX:]:
        if not isinstance(h, dict):
            continue
        q = re.sub(r"\s+", " ", str(h.get("q") or "")).strip()[:300]
        a = re.sub(r"\s+", " ", str(h.get("a") or "")).strip()[:800]
        did = [re.sub(r"\s+", " ", str(x)).strip()[:120] for x in (h.get("did") or [])][:8]
        if q:
            hist.append(f"- Earlier request: {q}\n  Your answer: {a or '(none)'}"
                        + (f"\n  The user then did: {'; '.join(did)}" if did else ""))
    user = base[1]["content"]
    if ctl:
        user += ("\n\nControls in window 1 you can operate (number, kind, label, current value, the section it is in):\n"
                 + "\n".join(f"[{r}] {role} \"{lab}\"" + (f" = \"{val}\"" if val else "")
                              + (f" (in \"{near}\")" if near else "") for r, role, lab, val, near in ctl))
    if hist:
        user = "Earlier in this panel:\n" + "\n".join(hist) + "\n\nThe window AS IT IS NOW:\n\n" + user
    if today:
        user = f"Today is {today}.\n\n" + user
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _clean(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _json_object(text: str):
    """The model's JSON object, repaired where a small local model leaves it ALMOST valid.

    "Do it for me is completely useless": measured with the node's own model, a reply proposing two
    correct steps (fill the search box, press New note) ended without its final "}" -- json.loads
    refused it, every step was thrown away and the panel showed the raw JSON as the answer. So: strip
    code fences, cut from the first "{", close what is still open (outside strings), drop trailing
    commas, and parse that. Anything still unparseable is None."""
    import json
    t = str(text or "")
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", t.strip(), flags=re.I)
    i = t.find("{")
    if i < 0:
        return None
    t = t[i:]
    try:
        obj, _end = json.JSONDecoder().raw_decode(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    stack, in_str, esc = [], False, False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    fixed = t + ('"' if in_str else "") + "".join(reversed(stack))
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    try:
        obj = json.loads(fixed)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# What small models write instead of the step kind asked for -> the kind they meant.
_KIND_ALIASES = {"type": "fill", "enter": "fill", "input": "fill", "write": "fill", "set": "fill",
                 "select": "choose", "pick": "choose", "check": "toggle", "tick": "toggle",
                 "uncheck": "toggle", "tap": "click", "open_link": "click", "follow": "click"}


def parse_steps(text: str, commands: bool = False, want_tasks: bool = False, controls=None) -> dict:
    """The model's reply -> {answer, tasks, steps}, every field validated. A reply that is not JSON is
    still an answer (local models ignore formats), and its "- " lines become tasks when tasks were asked."""
    raw = _json_object(text)
    if not isinstance(raw, dict):
        answer = str(text or "").strip()[:4000]
        # A reply that LOOKS like the JSON we asked for but cannot be read is not an answer to show:
        # a wall of braces reads as the feature being broken. Say so in words.
        if answer.lstrip().startswith(("{", "```")):
            answer = "The AI's reply could not be read — press the button again."
        tasks = []
        if want_tasks:
            for line in answer.splitlines():
                bullet = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", line)
                if bullet and bullet.group(1).strip():
                    tasks.append({"text": _clean(bullet.group(1), 200), "due": "", "who": ""})
        return {"answer": answer, "tasks": tasks[:TASK_MAX], "steps": []}
    answer = str(raw.get("answer") or "").strip()[:4000]
    tasks = []
    for t in raw.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        txt = _clean(t.get("text"), 200)
        if not txt:
            continue
        due = _clean(t.get("due"), 10)
        tasks.append({"text": txt, "due": due if _DATE.match(due) else "", "who": _clean(t.get("who"), 60)})
        if len(tasks) >= TASK_MAX:
            break
    steps = []
    refs = {r: (role, lab) for r, role, lab, _v, _n in clean_controls(controls)}
    for st in raw.get("steps") or []:
        if not isinstance(st, dict):
            continue
        kind = _clean(st.get("do"), 20).lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if refs and kind in ("click", "press"):
            try:
                _r = int(st.get("ref"))
            except (TypeError, ValueError):
                _r = None
            _txt = str(st.get("text") or "").strip()
            # "click" on a text box WITH text is typing into it; "press" naming the control itself is a
            # click. The model means the obvious thing; read it that way. A press of a key that is not
            # allowed (Delete, …) is still dropped below.
            if kind == "click" and _r in refs and refs[_r][0] in ("textbox", "textarea", "search", "input") and _txt:
                kind = "fill"
            elif (kind == "press" and _r in refs
                  and (not _txt or _txt.lower() == refs[_r][1].lower())):
                kind = "click"      # "press New note" on the New note button; any other key stays refused
        if kind == "scroll" and refs:
            way = str(st.get("text") or "").strip().lower()
            if way in ("down", "up"):
                steps.append({"do": "scroll", "ref": 0, "target": "", "label": _clean(st.get("label"), 50)
                              or f"Scroll {way}", "text": way, "on": False})
            if len(steps) >= ACT_STEP_MAX:
                break
            continue
        if kind in ACT_KINDS:
            # Only a control the client actually listed -- a number the model made up is dropped.
            try:
                ref = int(st.get("ref"))
            except (TypeError, ValueError):
                continue
            if ref not in refs:
                continue
            role, lab = refs[ref]
            txt = str(st.get("text") or "").strip()[:2000]
            if kind in ("fill", "choose") and not txt:
                continue
            # "[Your note title here]" is the model not knowing what to type, not something to type.
            if kind == "fill" and re.fullmatch(r"\s*[\[<{(].*[\]>})]\s*|.*\byour\b.*\bhere\b.*", txt, re.I | re.S):
                continue
            if kind == "press":
                txt = next((k for k in PRESS_KEYS if k.lower() == txt.lower()), "")
                if not txt:
                    continue
            on = st.get("on")
            on = bool(on) if isinstance(on, bool) else str(on).strip().lower() in ("1", "true", "yes", "on")
            default = {"click": f"Press “{lab}”", "fill": f"Fill “{lab}”", "choose": f"Choose in “{lab}”",
                       "toggle": f"{'Tick' if on else 'Untick'} “{lab}”", "press": f"Press {txt} in “{lab}”"}[kind]
            steps.append({"do": kind, "ref": ref, "target": lab, "label": _clean(st.get("label"), 50) or default,
                          "text": txt, "on": on})
            if len(steps) >= ACT_STEP_MAX:
                break
            continue
        if kind not in STEP_KINDS or (kind == "command" and not commands):
            continue
        txt = str(st.get("text") or "").strip()
        if kind == "command":
            txt = txt.replace("\r", "").strip()
            if txt.startswith("$ "):
                txt = txt[2:]
            if not txt or "\n" in txt or len(txt) > 400 or re.search(r"[\x00-\x1f\x7f]", txt):
                continue
        elif kind == "open":
            txt = _clean(txt, 30).lower()
            if txt not in OPEN_VIEWS:
                continue
        elif kind == "search":
            txt = _clean(txt, 200)
        else:
            txt = txt[:2000]
        if not txt:
            continue
        default = {"command": "Run command", "insert": "Insert", "note": "Save to Notes",
                   "calendar": "Add to Calendar", "open": "Open " + OPEN_VIEWS.get(txt, ""),
                   "search": "Search the web"}[kind]
        steps.append({"do": kind, "label": _clean(st.get("label"), 50) or default, "text": txt})
        if len(steps) >= (ACT_STEP_MAX if refs else STEP_MAX):
            break
    if not answer and not tasks and not steps:
        answer = str(text or "").strip()[:4000]
    return {"answer": answer, "tasks": tasks, "steps": steps}


_TASKY = re.compile(r"\b(task|tasks|to-?do|action items?|follow[- ]?ups?|deadlines?|commitments?)\b", re.I)


async def window_steps(db, user, windows, instruction: str, history=None, commands: bool = False,
                       today: str = "", controls=None) -> dict:
    context = window_context(windows)
    out = await _chat(db, user, build_steps_messages(context, instruction, history, commands, today, controls), 0.2)
    if not out:
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    res = parse_steps(out, commands, bool(_TASKY.search(str(instruction or ""))), controls)
    logger.info("[chat-assist] window steps: %d windows -> %d tasks, %d steps",
                len(context), len(res["tasks"]), len(res["steps"]))
    return res


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
