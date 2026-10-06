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
ACTIONS = ("reply", "summarize", "links", "window", "window_event", "window_steps", "window_recipe")

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
           "placeholder such as [your title]; if you do not know the text, ask. "
           "Posting and replying are different: to POST something new, fill the main text box and press the "
           "button that sends it (Post, Send, Save) -- not one that opens a new empty box. Only when the user "
           "asks to REPLY to (or comment on, or quote) a particular post: ONE \"fill\" step on THAT post's own "
           "Reply button, with the text -- the box it opens is filled for you; never type a reply into another "
           "box. To FIND something, type it into the window's own search box when it has one. An \"item\" is a row "
           "in a list (an email, a message, a file): click it to OPEN it. A checkbox labelled \"Select\" only ticks "
           "the row it is in, for acting on several at once -- it never opens anything. To SHOW or GO TO a part "
           "of the window that sits behind a tab, folder or section button, click that control; do not describe "
           "what is on screen instead. To ADD something (a contact, an event, a bill) whose form is not among the "
           "controls, click the button that opens it (+ Contact, + Event, New ...) as the LAST step: its fields "
           "are shown to you next and you fill them then. Text the user gives "
           "in quotes is used exactly as written. Never propose deleting, removing or "
           "clearing anything unless the user asked for exactly that. " if ctl else "")
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
        return _salvage_fields(t)


def _salvage_fields(t: str):
    """ONE BROKEN STEP MUST NOT COST THE TASKS BESIDE IT. Measured ("Extract decisions and next actions",
    2/4): the model found both tasks, then wrote a step as `{"do": "open", "label": "…"}, "ref": 9, …}` --
    the whole object refused to parse and the correct tasks were thrown away with it. Read "answer" as a
    string and each object inside "tasks" / "steps" on its own, keeping the ones that parse."""
    import json
    out = {}
    m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', t)
    if m:
        try:
            out["answer"] = json.loads('"' + m.group(1) + '"')
        except Exception:
            pass
    for key in ("tasks", "steps"):
        m = re.search(r'"' + key + r'"\s*:\s*\[', t)
        if not m:
            continue
        items, depth, start, in_str, esc = [], 0, None, False, False
        for j in range(m.end(), len(t)):
            ch = t[j]
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
            elif ch == "{":
                if depth == 0:
                    start = j
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        o = json.loads(re.sub(r",\s*}", "}", t[start:j + 1]))
                        if isinstance(o, dict):
                            items.append(o)
                    except Exception:
                        pass
                    start = None
            elif ch == "]" and depth == 0:
                break
        out[key] = items
    return out or None


# What small models write instead of the step kind asked for -> the kind they meant.
_KIND_ALIASES = {"type": "fill", "enter": "fill", "input": "fill", "write": "fill", "set": "fill",
                 "select": "choose", "pick": "choose", "check": "toggle", "tick": "toggle",
                 "uncheck": "toggle", "tap": "click", "open_link": "click", "follow": "click"}


def parse_steps(text: str, commands: bool = False, want_tasks: bool = False, controls=None,
                instruction: str = "") -> dict:
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
    keyed = None
    refs = {r: (role, lab) for r, role, lab, _v, _n in clean_controls(controls)}
    for st in raw.get("steps") or []:
        if not isinstance(st, dict):
            continue
        kind = _clean(st.get("do"), 20).lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if refs and kind == "open" and str(st.get("ref") or "").strip().isdigit() and int(st.get("ref")) in refs:
            kind = "click"          # {"do":"open","ref":1} -- the model opening a tab, not an app (measured)
        if refs and kind in ("press", "fill"):
            # A KEYPAD (Calculator). "fill [Backspace] with 12" and "press ×" are the model typing on keys;
            # every character that is the label of a one-character button becomes a press of that button.
            _t = str(st.get("text") or "").strip().replace("*", "×").replace("x", "×").replace("/", "÷").replace("-", "−")
            _keys = {lab: r for r, (role, lab) in refs.items() if role == "button" and len(lab) == 1}
            try:
                _rr = int(st.get("ref"))
            except (TypeError, ValueError):
                _rr = None
            if (len(_keys) >= 10 and _t and _rr in refs and refs[_rr][0] not in _TEXTBOX_ROLES
                    and all(ch in _keys for ch in _t.replace(" ", ""))):
                keyed = _keys
                for ch in _t.replace(" ", ""):
                    steps.append({"do": "click", "ref": _keys[ch], "target": ch, "label": "Press " + ch, "text": "", "on": False})
                continue
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
            # An ELIDED token is a made-up stand-in too: "magnet:xt9:...", "0x1234…". Put on a button it was
            # the model pressing the button and inventing what goes in the box -- press it, type nothing.
            if kind == "fill" and re.fullmatch(r"\S*(\.\.\.|…)\S*", txt):
                if role in _TEXTBOX_ROLES:
                    continue
                kind, txt = "click", ""
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
    if keyed and "=" in keyed and steps and steps[-1].get("target") != "=":
        steps.append({"do": "click", "ref": keyed["="], "target": "=", "label": "Press =", "text": "", "on": False})
    steps = steps[:ACT_STEP_MAX * 3] if keyed else steps
    steps = _repair_steps(steps, refs, instruction, {r: n for r, _ro, _l, _v, n in clean_controls(controls)})
    if not answer and not tasks and not steps:
        answer = str(text or "").strip()[:4000]
    return {"answer": answer, "tasks": tasks, "steps": steps}


# Measured with this node's own model (scripts/eval_window_ai.py), the two ways a correct plan came out
# wrong, repaired here so they do not depend on a 9B model following an instruction:
#  * REPLY, THEN TYPE SOMEWHERE ELSE. The box a Reply/Comment/Quote button opens does not exist yet, so
#    the model types into a text box that does -- on a timeline, the NEW POST composer. Pressed as
#    proposed, that publishes a post instead of replying. One fill on the opener is what it means, and
#    the panel performs a fill on a button as "press it, then type into the box it opened".
#  * DESTRUCTION NOBODY ASKED FOR. "clean this up a bit" in Notes proposed "Delete all notes & files".
#    A step that deletes, removes, clears or empties is kept only when the request itself says so.
_OPENS_A_BOX = re.compile(r"\b(reply|comment|quote|respond|answer|message|dm)\b", re.I)
_TEXTBOX_ROLES = ("textbox", "textarea", "search", "input")
# 'single', "double" or “curly” -- an apostrophe inside a word (I'll, don't) is not a quote.
_QUOTED = re.compile(r"(?<![\w])'([^'\n]{1,500})'(?![\w])|\"([^\"\n]{1,500})\"|“([^”\n]{1,500})”")
_DESTROYS = re.compile(r"\b(delete|remove|erase|wipe|trash|discard|clear|empty|reset|destroy|purge|unfollow|block|leave)\b", re.I)


_NAVIGATES = re.compile(r"\b(show|open|go(?: back)? to|switch to|take me to|view|see|display)\b", re.I)
_OPENS_ROW = re.compile(r"\b(open|read|show|view|look at|see)\b", re.I)
_TICKS_ROW = re.compile(r"\b(select|tick|check|mark)\b", re.I)
_WRITES = re.compile(r"\b(write|type|say|post|reply|respond|answer|fill|enter|set|change|add|put|rename|create|make|compose|draft|edit|update|insert|paste)\b", re.I)
_FINDS = re.compile(r"\b(?:search|find|look)\b.*?\b(?:for|about)\s+(.+)$", re.I)
_COMMON_WORDS = {"open", "read", "show", "view", "look", "email", "emails", "mail", "message", "messages", "post", "posts",
                 "please", "that", "this", "with", "from", "about", "have", "what", "which", "where", "there", "their",
                 "them", "they", "your", "mine", "into", "onto", "want", "would", "could", "should", "select", "click",
                 "file", "files", "note", "notes", "item", "items", "list", "the"}
_RISKY_NAV = re.compile(r"\b(send|post|publish|pay|delete|remove|log ?out|sign ?out|reset|wipe|private key|nsec)\b", re.I)


def _repair_steps(steps: list, refs: dict, instruction: str, near: dict | None = None) -> list:
    near = near or {}
    low = str(instruction or "").lower()
    # A ROW AND ITS "SELECT" BOX ARE TWO DIFFERENT THINGS. Measured in Mail: asked to "open the receipt
    # email" the model ticked a row's Select box (0/3), asked to "select the lunch email" it opened the
    # row (0/3). The box and the row it belongs to share their `near` (the row's text), so the person's
    # verb decides which of the pair the step lands on.
    def _twin(ref, want_role):
        n = near.get(ref)
        if not n:
            return None
        hit = [r for r, (role, lab) in refs.items() if r != ref and near.get(r) == n
               and (role == want_role if want_role != "select" else (role in ("checkbox", "radio") and lab.lower() == "select"))]
        return hit[0] if len(hit) == 1 else None
    opens, ticks = bool(_OPENS_ROW.search(low)), bool(_TICKS_ROW.search(low))
    for st in steps:
        role, lab = refs.get(st.get("ref"), ("", ""))
        if opens and not ticks and st.get("do") in ("click", "toggle") and role in ("checkbox", "radio") and lab.lower() == "select":
            t = _twin(st["ref"], "item")
            if t:
                st.update(do="click", ref=t, target=refs[t][1], on=False)
        elif ticks and not opens and st.get("do") == "click" and role == "item":
            t = _twin(st["ref"], "select")
            if t:
                st.update(do="toggle", ref=t, target=refs[t][1], on=True)
    out = []
    for st in steps:
        prev = out[-1] if out else None
        if (st.get("do") == "fill" and prev and prev.get("do") == "click"
                and refs.get(st.get("ref"), ("",))[0] in _TEXTBOX_ROLES
                and _OPENS_A_BOX.search(str(prev.get("target") or ""))
                and refs.get(prev.get("ref"), ("",))[0] not in _TEXTBOX_ROLES):
            out[-1] = dict(prev, do="fill", text=st["text"], label=prev.get("label") or st.get("label"))
            continue
        out.append(st)
    # THE PERSON'S OWN WORDS. "post 'good morning nostr'" came back as "Good morning! How's everyone doing
    # today?" -- a model rewriting what it was told to type. One quoted phrase and one thing to type it
    # into: that is what goes in.
    quoted = _QUOTED.findall(str(instruction or ""))
    fills = [st for st in out if st.get("do") == "fill"]
    if len(quoted) == 1 and len(fills) == 1:
        q = next(x for x in quoted[0] if x).strip()
        if q:
            fills[0]["text"] = q[:2000]
    # THE BUTTON THE STEP NAMES. A plan to post said {"do":"click","ref":24,"label":"Post"} -- and 24 was
    # "New post", which opens an empty box and sends nothing. When a click's own label is EXACTLY the label
    # of one control and not of its target, the model has said which button it meant.
    by_label = {}
    for r, (_role, lab) in refs.items():
        by_label.setdefault(str(lab).strip().lower(), []).append(r)
    for i, st in enumerate(out):
        said = str(st.get("label") or "").strip().lower()
        # Enter on a BUTTON is a click (measured: {"do":"press","ref":24 "New post","text":"Enter","label":"Post"}).
        enter_on_button = (st.get("do") == "press" and str(st.get("text") or "").lower() == "enter"
                           and refs.get(st.get("ref"), ("",))[0] not in _TEXTBOX_ROLES)
        prev = out[i - 1] if i else None
        if enter_on_button and prev and prev.get("do") == "fill" and refs.get(prev.get("ref"), ("",))[0] in _TEXTBOX_ROLES:
            # ...except straight after typing: "fill Search contacts 'bob', press Enter on [2]" meant the
            # box it just typed in, and [2] was "+ Contact" (measured: the search became a new contact).
            st.update(ref=prev["ref"], target=prev.get("target"))
            continue
        if enter_on_button:
            st.update(do="click", text="")
        if st.get("do") == "click" and said and said != str(st.get("target") or "").strip().lower():
            hit = by_label.get(said) or []
            if len(hit) == 1:
                st.update(ref=hit[0], target=refs[hit[0]][1])
    # MEASURED SHAPES ON BUTTONS (eval 2026-10-06), each read as the obvious thing:
    #  * "toggle" on a BUTTON ("Mark unread") is pressing it;
    #  * "fill" on a button that opens no box ("Add torrent" filled with an invented magnet) is pressing it
    #    -- the text was made up; Reply/Comment keep their fill, which types into the box they open;
    #  * a fill whose own label is exactly another TEXT BOX's label ("Note title", put on Note text) meant
    #    that box;
    #  * pressing a button again right after filling it ("Send reply" on the Reply just filled) is dropped:
    #    the fill already pressed it, and a second press closes what it opened.
    boxes_by_label = {}
    for r, (role, lab) in refs.items():
        if role in _TEXTBOX_ROLES:
            boxes_by_label.setdefault(str(lab).strip().lower(), []).append(r)
    kept = []
    for st in out:
        role = refs.get(st.get("ref"), ("",))[0]
        if st.get("do") == "toggle" and role in ("button", "link", "tab"):
            st.update(do="click", on=False)
        elif st.get("do") == "fill" and role in ("button", "link", "tab") and not _OPENS_A_BOX.search(str(st.get("target") or "")):
            st.update(do="click", text="")
        elif st.get("do") == "fill" and role in _TEXTBOX_ROLES:
            hit = boxes_by_label.get(str(st.get("label") or "").strip().lower()) or []
            if len(hit) == 1 and hit[0] != st["ref"]:
                st.update(ref=hit[0], target=refs[hit[0]][1])
        prev = kept[-1] if kept else None
        if (prev and prev.get("do") == "fill" and st.get("do") in ("click", "press") and st.get("ref") == prev.get("ref")
                and refs.get(prev.get("ref"), ("",))[0] not in _TEXTBOX_ROLES):
            continue
        kept.append(st)
    out = kept
    # THE TAB THEY NAMED. "show me what's trending" opened Nostrverse, beside a tab labelled Trending.
    # When the request names exactly one tab by its label, a tab click goes to that tab.
    low = str(instruction or "").lower()
    named = [r for r, (role, lab) in refs.items()
             if role == "tab" and lab and re.search(r"\b" + re.escape(lab.lower()) + r"\b", low)]
    if len(named) == 1:
        for st in out:
            if st.get("do") == "click" and refs.get(st.get("ref"), ("",))[0] == "tab" and st["ref"] != named[0]:
                st.update(ref=named[0], target=refs[named[0]][1])
    def _says(lab):
        w = re.sub(r"^[^\w]+", "", lab.lower()).strip()      # "⬇ Downloads" is named "downloads"
        forms = {w, w[:-1] if w.endswith("s") and len(w) > 3 else w}
        return any(f and re.search(r"\b" + re.escape(f) + r"\b", low) for f in forms)
    def _click(r):
        return {"do": "click", "ref": r, "target": refs[r][1], "label": refs[r][1][:40], "text": "", "on": False}
    # FIND MEANS THE SEARCH BOX. "search my email for invoices" opened the invoice email instead (0/3). With
    # a search box on screen and "search/find/look ... for X", the plan is: type X there and run it.
    m = _FINDS.search(low)
    boxes = [r for r, (role, lab) in refs.items() if role in _TEXTBOX_ROLES and re.search(r"\b(search|filter|find)", lab.lower())]
    if m and len(boxes) == 1 and not any(st.get("do") == "fill" and st.get("ref") == boxes[0] for st in out):
        q = m.group(1).strip(" .?!'\"")
        if q:
            out = [{"do": "fill", "ref": boxes[0], "target": refs[boxes[0]][1], "label": "Search " + q[:30], "text": q[:200], "on": False},
                   {"do": "press", "ref": boxes[0], "target": refs[boxes[0]][1], "label": "Run search", "text": "Enter", "on": False}]
    # THE KIND OF RESULT THEY NAMED. "find news about bitcoin" typed bitcoin and searched the WEB, beside a
    # News button (0/3). A button named in the words BEFORE the query is the category to search in.
    if m and len(boxes) == 1:
        before = low[:m.start(1)]
        cats = [r for r, (role, lab) in refs.items()
                if role in ("tab", "button") and 2 < len(lab) <= 15 and not _RISKY_NAV.search(lab)
                and re.sub(r"^[^\w]+", "", lab.lower()).strip() not in ("search", "find", "go")
                and re.search(r"\b" + re.escape(re.sub(r"^[^\w]+", "", lab.lower()).strip()) + r"\b", before)]
        if len(cats) == 1 and not any(st.get("ref") == cats[0] for st in out):
            out.append(_click(cats[0]))
    # THE ROW THEY NAMED. "open the receipt email" opened the invoice email beside a row that says receipt
    # (1/3). A distinctive word of the request found in exactly ONE row decides which row a row-click is.
    words = [w for w in re.findall(r"[a-z0-9']{4,}", low) if w not in _COMMON_WORDS]
    rows = [r for r, (role, lab) in refs.items() if role == "item"]
    for w in words:
        hit = [r for r in rows if re.search(r"\b" + re.escape(w), refs[r][1].lower())]
        if len(hit) == 1:
            for st in out:
                if st.get("do") == "click" and refs.get(st.get("ref"), ("",))[0] == "item" and st["ref"] != hit[0]:
                    st.update(ref=hit[0], target=refs[hit[0]][1])
            break
    # LOOKING TYPES NOTHING. "show my relay settings" once came back as a fill of the Instance box with a
    # relay URL the model made up -- run by Do all, that repoints the app at a stranger's server. A request
    # only to show / open / go somewhere, with nothing quoted and no verb that writes, has no text to type.
    if _NAVIGATES.search(low) and not _FINDS.search(low) and not quoted and not _WRITES.search(low):
        out = [st for st in out if st.get("do") != "fill"]
    # SHOW ME X, WHERE X IS A TAB. "show my relay settings" came back as a description of what was already
    # on screen (0/3), or a click on a neighbouring control, beside a tab labelled Relays; "show me what I
    # sent" came back as a scroll. A request to go somewhere that names exactly one tab/button, answered with
    # nothing but clicks and scrolls elsewhere, is a click on the one it named. Never a risky control.
    if _NAVIGATES.search(low) and all(st.get("do") in ("click", "scroll") for st in out):
        named = [r for r, (role, lab) in refs.items()
                 if role in ("tab", "button", "link") and lab and len(lab) <= 30 and not _RISKY_NAV.search(lab) and _says(lab)]
        if len(named) == 1 and not any(st.get("ref") == named[0] for st in out):
            out = [_click(named[0])]
    # THE BUTTON THE REQUEST IS NAMED AFTER. "add my paycheck of 2000 as income" filled the name, the amount
    # and ticked Income -- and never pressed Add (0/3); "post 'good morning'" typed and stopped. When the
    # request starts with the verb that exactly ONE button is labelled, and the plan typed something
    # for it, the plan ends by pressing it. Never a destructive verb (that stays the person's own press).
    verb = re.match(r"\s*(?:please\s+)?(add|post|send|save|create)\b", low)
    if verb and any(st.get("do") == "fill" for st in out):
        same = [r for r, (role, lab) in refs.items() if role == "button" and lab.strip().lower() == verb.group(1)]
        if len(same) == 1 and not any(st.get("ref") == same[0] and st.get("do") in ("click", "press") for st in out):
            out.append(_click(same[0]))
    asked_to_destroy = bool(_DESTROYS.search(str(instruction or "")))
    return [st for st in out if asked_to_destroy or not _DESTROYS.search(str(st.get("target") or "") + " " + str(st.get("label") or ""))]


_TASKY = re.compile(r"\b(task|tasks|to-?do|action items?|follow[- ]?ups?|deadlines?|commitments?)\b", re.I)


async def window_steps(db, user, windows, instruction: str, history=None, commands: bool = False,
                       today: str = "", controls=None) -> dict:
    context = window_context(windows)
    out = await _chat(db, user, build_steps_messages(context, instruction, history, commands, today, controls), 0.2)
    if not out:
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    res = parse_steps(out, commands, bool(_TASKY.search(str(instruction or ""))), controls, instruction)
    logger.info("[chat-assist] window steps: %d windows -> %d tasks, %d steps",
                len(context), len(res["tasks"]), len(res["steps"]))
    return res


# ---- the ✨ panel's own buttons: RECIPES, not plans ----------------------------------------------------
# Measured against the node's own model (2026-10-06), the buttons were canned prompts sent through free-form
# step planning, and what came back was not just weak but unsafe: Email's "Draft reply" typed invented
# payment details ("Wire $45 to account ending in 8921") into the ACCOUNT field, "Extract tasks" proposed
# SENDING a reply, Messages' "Draft reply" answered "What should I say?" with no conversation open, and
# Notes' "Do it for me" searched for its own instruction. ("we need the actions to actually be useful")
#
# A recipe has ONE fixed outcome. The model writes only the TEXT -- plain prose, which a small model does
# far more reliably than a JSON plan -- and the buttons around it are built here, from what the client
# says is on screen, so they cannot wander: a summary can be saved to Notes, a draft goes into the reply
# box (after pressing the window's own Reply when the box is not open yet), a tidied note replaces the
# note with Undo, tasks get "Add to Calendar". Nothing a recipe produces can send, delete or navigate.
RECIPES = ("summary", "tasks", "draft", "tidy", "checklist", "explain")
RECIPE_TEXT_MAX = 8000
_NO_INVENT = ("Use ONLY what the window shows. Never invent facts, amounts, account or card numbers, "
              "addresses, phone numbers, dates, times, names or promises.")
# Only a DRAFT may leave a gap for the person to fill; asked of a summary, the model printed "[placeholder]"
# as if it were content (measured).
_GAPS = " Where the user has to supply something you do not know, write a short [placeholder] in square brackets."
_PREAMBLE = re.compile(r"^\s*(?:sure[,!.]?\s*)?(?:here(?:'s| is)[^:\n]{0,60}:|draft(?: reply)?:|reply:|"
                       r"summary:|rewritten(?: note)?:|checklist:)\s*", re.I)


def _windows_block(context: list) -> str:
    if not context:
        raise AssistError(400, "There is no window to ask about.")
    parts = []
    for i, (title, kind, label, text) in enumerate(context, 1):
        body = text if text else "(no text available -- only the window's name is known)"
        parts.append(f"Window {i}: \"{title}\" ({kind}), {label}:\n<<<WINDOW\n{body}\nWINDOW")
    return "\n\n".join(parts)


def build_recipe_messages(context: list, recipe: str, text: str = "", note: str = "") -> list:
    """The messages for one recipe. `text` is the current value of the window's own text box (a note being
    tidied); `note` is anything the person typed to steer it ("shorter", "in Spanish")."""
    recipe = str(recipe or "")
    if recipe not in RECIPES or recipe == "tasks":
        raise AssistError(400, "Unknown action.")
    text = str(text or "").strip()[:RECIPE_TEXT_MAX]
    steer = re.sub(r"\s+", " ", str(note or "")).strip()[:INSTRUCTION_MAX]
    lang = " Write in the language of the content."
    if recipe == "explain":
        # A FIXED QUESTION about this window, chosen by the button ("What's coming up", "Suggest my next
        # move", "Explain these numbers"); the button's text arrives as `note`. Same rules as a summary.
        if not steer:
            raise AssistError(400, "Ask something about the window first.")
        system = ("Answer the question below for the person looking at this window, from what it shows: "
                  "short, plain sentences and \"- \" bullets, the most useful thing first. If the window does "
                  "not show enough to answer, say exactly what is missing instead of guessing. "
                  + _NO_INVENT + lang)
        user = _windows_block(context) + "\n\nQuestion: " + steer
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if recipe == "summary":
        system = ("Summarize what this window shows, for the person looking at it: the 3 to 7 points that "
                  "matter most, each as a \"- \" bullet, saying who said or asked what. Lead with anything "
                  "that needs them (a question to answer, a deadline, a request). No preamble, no closing line. "
                  + _NO_INVENT + lang)
    elif recipe == "draft":
        system = ("Write the reply the user would send next in the conversation in this window: answer the "
                  "latest message addressed to them, in a natural first-person voice, short (1-4 sentences "
                  "unless the conversation needs more). Output ONLY the message text -- no greeting line "
                  "about being an AI, no quotes, no explanation, no subject line. " + _NO_INVENT + _GAPS + lang)
    else:
        if not text:
            raise AssistError(400, "There is no text in this window's box to work on.")
        shape = ("as a checklist: one \"- [ ] \" line per thing to do, grouped under short headings when "
                 "there are several topics" if recipe == "checklist" else
                 "clean and well organised: fix spelling and grammar, put related lines together, use short "
                 "headings and \"- \" bullets where they help")
        system = ("Rewrite the user's note " + shape + ". Keep EVERY fact, number, link and name exactly; add "
                  "nothing new and drop nothing that matters. Output ONLY the rewritten note." + lang)
    user = _windows_block(context)
    if text and recipe in ("tidy", "checklist"):
        user = "The note to rewrite:\n<<<NOTE\n" + text + "\nNOTE\n\n(For context, the window it is in:)\n" + user
    if steer:
        user += "\n\nAlso: " + steer
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def clean_recipe_text(out: str) -> str:
    """The model's prose, without a 'Here is your draft:' preamble or wrapping quotes/fences."""
    t = str(out or "").strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t, flags=re.I).strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S | re.I).strip()
    t = _PREAMBLE.sub("", t, count=1).strip()
    if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'“”":
        t = t[1:-1].strip()
    if t.startswith("“") and t.endswith("”"):
        t = t[1:-1].strip()
    return t[:RECIPE_TEXT_MAX]


def build_tasks_messages(context: list, today: str = "") -> list:
    """Tasks on their own prompt. Riding the step-planning one (controls, kinds, rules about Reply buttons)
    found the projector and the invoice in one run and NOTHING in the next, same window (measured)."""
    today = today if _DATE.match(str(today or "")) else ""
    system = ("List what the person looking at this window has to DO: every task, request made of them, "
              "follow-up, commitment and deadline in it -- one per item, nothing invented, nothing that is "
              "merely news. Reply with ONLY a JSON object: {\"tasks\": [{\"text\": one concrete action "
              "starting with a verb, \"due\": \"YYYY-MM-DD\" or \"\", \"who\": the person responsible if "
              "named, else \"\"}]}; {\"tasks\": []} when there is nothing to do. Resolve relative dates "
              "(\"Saturday\", \"tomorrow\") against today's date. " + _NO_INVENT)
    return [{"role": "system", "content": system},
            {"role": "user", "content": (f"Today is {today}.\n\n" if today else "") + _windows_block(context)}]


_CHECK_LINE = re.compile(r"^(\s*)(?:[-*•]|\d+[.)])\s+(?!\[[ xX]\])")


def as_checklist(text: str) -> str:
    """Every list line a '- [ ] ' line, however the model bulleted it (it often wrote plain '- ')."""
    return "\n".join(_CHECK_LINE.sub(r"\1- [ ] ", line) for line in str(text or "").split("\n"))


def recipe_steps(recipe: str, body: str, reply_ref=None, box_ref=None, box_label: str = "") -> list:
    """The buttons a recipe's result gets -- decided here, never by the model."""
    if recipe == "summary":
        return [{"do": "note", "label": "Save summary to Notes", "text": body}]
    if recipe == "explain":
        return [{"do": "note", "label": "Save to Notes", "text": body}]
    if recipe == "draft":
        # ONE step. Into the reply box when it is open; else a "fill" on the window's own Reply control,
        # which the client already turns into "press it, then type into the box it opens" -- never sent.
        # With neither, the text is offered to copy.
        ref = box_ref if isinstance(box_ref, int) and box_ref > 0 else reply_ref
        if isinstance(ref, int) and ref > 0:
            return [{"do": "fill", "ref": ref, "target": _clean(box_label, 80) or "Reply",
                     "label": "Put it in the reply box", "text": body, "on": False}]
        return [{"do": "insert", "label": "Put it in the reply box", "text": body}]
    if recipe in ("tidy", "checklist") and isinstance(box_ref, int) and box_ref > 0:
        return [{"do": "fill", "ref": box_ref, "target": _clean(box_label, 80) or "the note",
                 "label": "Replace the note with this", "text": body, "on": False}]
    return []


async def window_recipe(db, user, windows, recipe: str, today: str = "", text: str = "", note: str = "",
                        reply_ref=None, box_ref=None, box_label: str = "") -> dict:
    recipe = str(recipe or "")
    context = window_context(windows)
    if recipe == "tasks":
        out = await _chat(db, user, build_tasks_messages(context, today), 0.2)
        if not out:
            raise AssistError(502, "The AI did not come up with an answer — try again.")
        res = parse_steps(out, False, True, None, "tasks")
        tasks = res["tasks"]
        answer = (f"{len(tasks)} thing{'s' if len(tasks) != 1 else ''} to do." if tasks
                  else "Nothing to do in this window — no tasks, follow-ups or deadlines.")
        logger.info("[chat-assist] window recipe tasks: %d windows -> %d tasks", len(context), len(tasks))
        return {"answer": answer, "tasks": tasks, "steps": []}
    out = await _chat(db, user, build_recipe_messages(context, recipe, text, note), 0.3)
    body = clean_recipe_text(out)
    if recipe == "checklist":
        body = as_checklist(body)
    if not body:
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    logger.info("[chat-assist] window recipe %s: %d windows, %d chars in -> %d chars",
                recipe, len(context), sum(len(c[3]) for c in context) + len(str(text or "")), len(body))
    return {"answer": body, "tasks": [], "steps": recipe_steps(recipe, body, reply_ref, box_ref, box_label),
            "recipe": recipe}


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
