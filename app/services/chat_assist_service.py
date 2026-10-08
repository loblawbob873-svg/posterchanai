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
ACTIONS = ("reply", "summarize", "links", "window", "window_event", "window_steps", "window_recipe", "window_feed", "window_note", "window_contact", "window_calc")

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
    # A CONTINUE ROUND STILL HAS A JOB. Measured (eval multi-* cases, 0/21): handed "Continue: look at the
    # window as it is now…" with the real request only in the history, the model read "pressed New
    # contact" as the task being DONE -- "Bob Smith was added. The contact form is open for a new entry",
    # no steps, beside the empty form it had just opened. So the request IS the person's own request
    # again, with what a step that opened a form did and did not do said in words.
    goal = goal_of(instruction, history)
    if goal != instruction:
        instruction = (f"{goal}\n(Continuing this request. The window has changed since the steps above ran -- a "
                       "form or dialog may have opened. Those steps did only what they say: opening a form does "
                       "not fill it or save it. Propose the steps STILL NEEDED to finish the request, with the "
                       "controls listed now. Only when the window shows it is finished, say so and propose nothing.)")
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
    # A KEY GIVEN TWICE IS BOTH HALVES. Measured: {"answer": …, "tasks": […], "steps": [three fills],
    # "steps": [press Send]} -- json keeps only the LAST "steps", so the three fills (the whole email) were
    # thrown away and the plan was a bare Send. Lists under a repeated key are joined, in order.
    def _pairs(items):
        out = {}
        for k, v in items:
            if k in out and isinstance(out[k], list) and isinstance(v, list):
                out[k] = out[k] + v
            else:
                out[k] = v
        return out
    try:
        obj, _end = json.JSONDecoder(object_pairs_hook=_pairs).raw_decode(t)
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
        obj = json.loads(fixed, object_pairs_hook=_pairs)
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


# THE REQUEST A CONTINUE ROUND IS STILL WORKING ON. A task that spans windows ("add Bob Smith 555-1234 to
# my contacts": press New contact, then fill the form that opened) reaches its second round with the panel's
# Continue sentence as the instruction and the person's real request only in `history`. Every repair below
# reads the person's words -- the quoted text to type verbatim, "add" ending on the Add button, whether
# deleting was asked for -- so on a Continue round they must read the ORIGINAL request, or the round that
# actually fills the form is the one round that gets none of them.
_CONTINUE = re.compile(r"^\s*continue\b", re.I)


def goal_of(instruction: str, history=None) -> str:
    if not _CONTINUE.match(str(instruction or "")):
        return instruction
    for h in reversed(list(history or [])):
        q = str(h.get("q") or "") if isinstance(h, dict) else ""
        if q.strip() and not _CONTINUE.match(q):
            return q
    return instruction


# What small models write instead of the step kind asked for -> the kind they meant.
_KIND_ALIASES = {"type": "fill", "enter": "fill", "input": "fill", "write": "fill", "set": "fill",
                 "select": "choose", "pick": "choose", "check": "toggle", "tick": "toggle",
                 "uncheck": "toggle", "tap": "click", "open_link": "click", "follow": "click",
                 # {"do":"copy","ref":1 "Copy npub"} (measured): pressing the window's own Copy button.
                 "copy": "click"}


def parse_steps(text: str, commands: bool = False, want_tasks: bool = False, controls=None,
                instruction: str = "", history=None) -> dict:
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
    for t in _list(raw.get("tasks")):
        if not isinstance(t, dict):
            continue
        txt = _clean(t.get("text"), 200)
        if not txt:
            continue
        due = _clean(t.get("due"), 10)
        tasks.append({"text": txt, "due": due if _DATE.match(due) else "", "who": _clean(t.get("who"), 60)})
        if len(tasks) >= TASK_MAX:
            break
    # THE TASKS IT WROTE AS BULLETS. Asked for decisions and next actions, the model listed them as "- "
    # lines in its answer and left "tasks" empty (measured, 2 runs in 3) -- the same reading the non-JSON
    # path above already gives a reply with no JSON at all.
    if want_tasks and not tasks:
        for line in answer.splitlines():
            bullet = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", line)
            if bullet and bullet.group(1).strip():
                tasks.append({"text": _clean(bullet.group(1), 200), "due": "", "who": ""})
        tasks = tasks[:TASK_MAX]
    steps = []
    keyed = None
    refs = {r: (role, lab) for r, role, lab, _v, _n in clean_controls(controls)}
    for st in _list(raw.get("steps")):
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
            # A ref that names no control at all ("fill ref 0 with 12", measured) is still the keypad: the
            # digits are what the person asked for, and dropping them computed "× 7 =".
            if (len(_keys) >= 10 and _t and (_rr not in refs or refs[_rr][0] not in _TEXTBOX_ROLES)
                    and all(ch in _keys for ch in _t.replace(" ", ""))):
                keyed = _keys
                for ch in _t.replace(" ", ""):
                    steps.append({"do": "click", "ref": _keys[ch], "target": ch, "label": "Press " + ch, "text": "", "on": False})
                continue
        if refs and kind == "click" and str(st.get("ref") or "").strip().isdigit() and refs.get(int(st.get("ref")), ("",))[0] == "list":
            # A CLICK ON A DROPDOWN NAMING AN OPTION is choosing it: {"do":"click","ref":3 "Second language",
            # "label":"Select Spanish"} (measured, 2 runs in 3) -- clicking a <select> does nothing a step can see.
            _opt = str(st.get("text") or "").strip() or re.sub(r"^(?:select|choose|pick|set(?: to)?|change to|switch to|use)\s+", "",
                                                               str(st.get("label") or "").strip(), flags=re.I)
            if _opt and _opt.lower() != refs[int(st.get("ref"))][1].lower():
                kind, st = "choose", dict(st, text=_opt)
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
            # On a BUTTON it is the model pressing the button and not knowing what goes in the box it opens
            # ("Add torrent" filled with "[magnet link]", measured) -- press it, type nothing.
            if kind == "fill" and re.fullmatch(r"\s*[\[<{(].*[\]>})]\s*|.*\byour\b.*\bhere\b.*", txt, re.I | re.S):
                if role in _TEXTBOX_ROLES or role == "list":
                    continue
                kind, txt = "click", ""
            # An ELIDED token is a made-up stand-in too: "magnet:xt9:...", "0x1234…". Put on a button it was
            # the model pressing the button and inventing what goes in the box -- press it, type nothing.
            if kind == "fill" and re.fullmatch(r"\S*(\.\.\.|…)\S*", txt):
                if role in _TEXTBOX_ROLES:
                    continue
                kind, txt = "click", ""
            if kind == "click":
                txt = ""            # a press carries no text (a "copy" step brought the npub along)
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
    steps = _keypad_from_request(steps, refs, str(goal_of(instruction, history) or ""))
    cc = clean_controls(controls)
    last = (list(history or [])[-1:] or [{}])[0] if _CONTINUE.match(str(instruction or "")) else {}
    steps = _repair_steps(steps, refs, goal_of(instruction, history), {r: n for r, _ro, _l, _v, n in cc},
                          {r: v for r, _ro, _l, v, _n in cc}, (last.get("did") if isinstance(last, dict) else None))
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


# "call this note Shopping list", "name it Taxes", "rename the folder to Q3" -> the name. It stops before
# what the thing should CONTAIN ("called Groceries with milk and eggs").
_NAMES = re.compile(r"\b(?:call(?:ed)?|name(?:d)?|title(?:d)?|rename)\b(?:\s+(?:this\s+\w+|the\s+\w+|this|it))?(?:\s+(?:to|as))?\s+"
                    r"(.+?)(?=\s+(?:with|containing|saying|that|and)\b|[,:;]|$)", re.I)
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
_ORDINAL = re.compile(r"\bthe\s+(first|second|third|fourth|fifth|last|[1-9](?:st|nd|rd|th))\b(?!\s+(?:time|thing))", re.I)
_READS = re.compile(r"\b(extract|summari[sz]e|explain|catch me up)\b", re.I)
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


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ISO_DAY = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CLOCK = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b", re.I)


def _clock_times(text: str) -> list:
    out = []
    for m in _CLOCK.finditer(text):
        if m.group(3):
            h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            out.append(f"{h:02d}:{m.group(2) or '00'}")
        else:
            out.append(f"{int(m.group(4)):02d}:{m.group(5)}")
    return out


def _put_facts(out: list, refs: dict, values: dict, request: str) -> list:
    """THE PERSON'S OWN DATES, TIMES AND ADDRESSES GO WHERE THE FORM HOLDS ONE. Measured on the New event
    form: "add a dentist appointment on 2026-10-09 from 3pm to 4pm" left Day at today's date and typed
    10:30-11:30 into From/To (and a "Downtown Dental Clinic" nobody mentioned); on New message, alice@x.test
    never reached To. A box already HOLDING a date (YYYY-MM-DD) or a time (HH:MM) is that kind of field, and
    a box labelled To/Email/Recipient takes an address: each value the request states goes into the one
    box of its kind (times in order: From, then To), replacing what the plan typed there. Applied only
    while the plan is filling the form in, so a request to look at a day types nothing."""
    if not any(st.get("do") == "fill" for st in out):
        return out
    boxes = sorted(r for r, (role, _l) in refs.items() if role in _TEXTBOX_ROLES)
    kinds = (
        (_ISO_DAY.findall(request), [r for r in boxes if _ISO_DAY.fullmatch(str(values.get(r) or "").strip())]),
        (_clock_times(request), [r for r in boxes if re.fullmatch(r"\d{2}:\d{2}", str(values.get(r) or "").strip())]),
    )
    # Addresses all go in the ONE address box, comma-separated -- never one each into To and Cc.
    mails, mail_boxes = _EMAIL.findall(request), [r for r in boxes if re.search(r"\b(to|e-?mail|recipients?)\b", refs[r][1], re.I)
                                                 and not re.search(r"\b(search|filter|find)", refs[r][1], re.I)
                                                 and not re.fullmatch(r"[\d:-]+", str(values.get(r) or "").strip())]
    if mails and len(mail_boxes) == 1:
        kinds += (([", ".join(dict.fromkeys(mails))], mail_boxes),)
    # A NAME IN TWO BOXES. "add Bob Smith 555-1234 to my contacts" filled Last = Smith and left First empty
    # (1 run in 3). With one First box and one Last box and half the name typed, the other half is the word
    # beside it in the request.
    def _one(rx):
        hit = [r for r in boxes if re.fullmatch(rx, _box_key(refs[r][1]))]
        return hit[0] if len(hit) == 1 else None
    first, last = _one(r"first( name)?|given name"), _one(r"last( name)?|surname|family name")
    if first and last:
        typed = {st.get("ref"): str(st.get("text") or "").strip() for st in out if st.get("do") == "fill"}
        if typed.get(last) and not typed.get(first):
            m = re.search(r"\b([A-Z][\w'-]*)\s+" + re.escape(typed[last]) + r"\b", request)
            if m:
                at = next(i for i, st in enumerate(out) if st.get("ref") == last)
                out.insert(at, {"do": "fill", "ref": first, "target": refs[first][1], "label": "First name", "text": m.group(1), "on": False})
        elif typed.get(first) and not typed.get(last):
            m = re.search(r"\b" + re.escape(typed[first]) + r"\s+([A-Z][\w'-]*)\b", request)
            if m:
                at = next(i for i, st in enumerate(out) if st.get("ref") == first) + 1
                out.insert(at, {"do": "fill", "ref": last, "target": refs[last][1], "label": "Last name", "text": m.group(1), "on": False})
    timed = False
    for vals, where in kinds:
        if not vals or not where or len(vals) > len(where):
            continue
        for v, r in zip(vals, where):
            have = [st for st in out if st.get("do") == "fill" and st.get("ref") == r]
            if have:
                have[-1]["text"] = v
            else:
                at = next((i for i, st in enumerate(out) if st.get("do") in ("click", "press")), len(out))
                out.insert(at, {"do": "fill", "ref": r, "target": refs[r][1], "label": f"Fill {refs[r][1][:30]}",
                                "text": v, "on": False})
        timed = timed or where is kinds[1][1]
    # Times stated and placed: an "All day" box the plan ticked would hide them.
    if timed:
        out = [st for st in out if not (st.get("do") == "toggle" and st.get("on") and re.search(r"all.?day", refs.get(st.get("ref"), ("", ""))[1], re.I))]
    # INVENTED EXTRAS. An optional field the request gave nothing for, filled with words it never used
    # ("Where (optional)" = "Downtown Dental Clinic"), is the model making something up.
    words = set(re.findall(r"[a-z0-9]{3,}", request.lower()))
    return [st for st in out if not (st.get("do") == "fill" and re.search(r"\(optional\)", refs.get(st.get("ref"), ("", ""))[1], re.I)
                                     and not (set(re.findall(r"[a-z0-9]{3,}", str(st.get("text") or "").lower())) & words))]


_ARITH_OPS = (("multiplied by", "×"), ("divided by", "÷"), ("times", "×"), ("plus", "+"), ("minus", "−"), ("over", "÷"),
              ("*", "×"), ("x", "×"), ("/", "÷"), ("+", "+"), ("-", "−"), ("×", "×"), ("÷", "÷"), ("−", "−"))
_ARITH = re.compile(r"\d+(?:\.\d+)?(?:\s*(?:multiplied by|divided by|times|plus|minus|over|[*x/+×÷−-])\s*\d+(?:\.\d+)?)+", re.I)


def _keypad_from_request(steps: list, refs: dict, request: str) -> list:
    """THE SUM THE PERSON TYPED IS THE SUM THE KEYPAD GETS. "calculate 12 times 7" on the Calculator came back
    as × 7 = (the 12 lost), 1 2 × = (the 7 lost), or 8 4 = (the ANSWER typed in as if it were the sum) --
    each a different near-miss from a model that copes badly with keypads. When the window is a keypad
    (ten or more one-character buttons and "=") and the request holds one arithmetic expression, the key
    presses ARE that expression, then "=" -- read from the request, not from the reply. Other steps stay."""
    keys = {lab: r for r, (role, lab) in refs.items() if role == "button" and len(lab) == 1}
    if len(keys) < 10 or "=" not in keys:
        return steps
    found = _ARITH.findall(request)
    if len(found) != 1:
        return steps
    expr, seq = found[0], []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch.isdigit() or ch == ".":
            seq.append(ch)
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        for word, sym in _ARITH_OPS:
            if expr[i:i + len(word)].lower() == word:
                seq.append(sym)
                i += len(word)
                break
        else:
            return steps
    if not all(k in keys for k in seq):
        return steps
    rest = [st for st in steps if st.get("ref") not in keys.values()]
    return rest + [{"do": "click", "ref": keys[k], "target": k, "label": "Press " + k, "text": "", "on": False} for k in seq + ["="]]


def _box_key(label) -> str:
    """A box's label reduced to its words: "To (comma-separated)" -> "to", "Write your message…" -> "write your message"."""
    t = re.sub(r"\([^)]*\)", " ", str(label or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w@.-]+", " ", t)).strip()


def _repair_steps(steps: list, refs: dict, instruction: str, near: dict | None = None, values: dict | None = None,
                  did=None) -> list:
    near = near or {}
    values = values or {}
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
        # (A fill on a DROPDOWN right after pressing Reply is the same mistake -- measured: the reply's text
        # aimed at Mail's Account list.)
        if (st.get("do") == "fill" and prev and prev.get("do") == "click"
                and refs.get(st.get("ref"), ("",))[0] in _TEXTBOX_ROLES + ("list",)
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
        bk = _box_key(refs.get(st.get("ref"), ("", ""))[1])
        names_button = bool(bk) and (_box_key(said) == bk or _box_key(said).startswith(bk + " "))
        if enter_on_button and names_button:
            # ...but Enter on "Save" whose own label says "Save contact" is pressing Save (measured).
            st.update(do="click", text="")
            continue
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
    kept, moved_from = [], {}
    for st in out:
        role = refs.get(st.get("ref"), ("",))[0]
        if st.get("do") == "toggle" and role in ("button", "link", "tab"):
            st.update(do="click", on=False)
        elif st.get("do") == "fill" and role in ("button", "link", "tab") and not _OPENS_A_BOX.search(str(st.get("target") or "")):
            st.update(do="click", text="")
        elif st.get("do") == "fill" and role in _TEXTBOX_ROLES:
            said = str(st.get("label") or "").strip().lower()
            hit = boxes_by_label.get(said) or []
            if not hit:
                # The step names the box in its own words: "First name" for the box labelled First, "To"
                # for "To (comma-separated)", "Phone" for "Phone number" (measured: Bob went into a phone's
                # "label" field, the subject into To). One box whose words start the same way.
                k = _box_key(said)
                hit = [r for r, (ro, lab) in refs.items() if ro in _TEXTBOX_ROLES and k and (
                       _box_key(lab) == k or k.startswith(_box_key(lab) + " ") or _box_key(lab).startswith(k + " "))]
            if len(hit) == 1 and hit[0] != st["ref"]:
                moved_from[hit[0]] = st["ref"]
                st.update(ref=hit[0], target=refs[hit[0]][1])
            elif not hit and st["ref"] in moved_from and not any(
                    k2.get("do") == "fill" and k2.get("ref") == moved_from[st["ref"]] for k2 in kept):
                # SWAPPED BOXES. Its neighbour was named onto THIS box and freed its own ("ref 6 'Subject'",
                # then "ref 5 'Message body'", with 5 Subject and 6 the body): the model had the two numbers
                # the wrong way round, so this one belongs in the box the other left.
                old_ref = moved_from[st["ref"]]
                st.update(ref=old_ref, target=refs[old_ref][1])
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
    # THE N-TH ONE. "open the second result" opened the first, 3 runs in 3. A request that counts ("the second
    # result", "the 3rd email", "the last one") moves a step on a row -- the row, or a control inside it --
    # to the same place in the row it counted to. Rows are the `item` controls, in screen order; a control
    # belongs to a row when it shares the row's `near`.
    rows = [r for r, (role, _l) in sorted(refs.items()) if role == "item" and near.get(r)]
    om = _ORDINAL.search(low)
    if om and len(rows) >= 2:
        w = om.group(1)
        n = len(rows) - 1 if w == "last" else _ORDINALS.get(w, int(re.sub(r"\D", "", w) or 0) - 1)
        if 0 <= n < len(rows):
            row_near = [near[r] for r in rows]
            for st in out:
                r = st.get("ref")
                if st.get("do") not in ("click", "press") or near.get(r) not in row_near:
                    continue
                i = row_near.index(near[r])
                if i == n:
                    continue
                mine = [x for x in sorted(refs) if near.get(x) == row_near[i]]
                theirs = [x for x in sorted(refs) if near.get(x) == row_near[n]]
                pos = mine.index(r)
                if pos < len(theirs) and refs[theirs[pos]][0] == refs[r][0]:
                    st.update(ref=theirs[pos], target=refs[theirs[pos]][1])
    # THE ROW'S OWN BUTTON. "save the Gentoo Wiki result to my notes" opened the result (2 runs in 3), beside
    # a "📓 Notes" button in that same row. A click on a row, when the request names one of the buttons that
    # row carries, is that button.
    for st in out:
        r = st.get("ref")
        if st.get("do") == "click" and refs.get(r, ("",))[0] == "item" and near.get(r):
            own = [x for x, (role, lab) in refs.items() if x != r and near.get(x) == near[r] and role in ("button", "link")
                   and not _RISKY_NAV.search(lab) and _says(lab)]
            if len(own) == 1:
                st.update(ref=own[0], target=refs[own[0]][1])
    # A NAME IS A TITLE. "call this note Shopping list" came back as a fill of the note's BODY with "Shopping
    # list" and nine grocery items nobody mentioned (2 runs in 3). A request that names the thing ("call it
    # X", "name it X", "title it X", "rename it to X") puts X -- and only X -- in the one box labelled as its
    # title or name; a fill elsewhere that starts with X was meant for that box.
    nm = _NAMES.search(str(instruction or ""))
    title_boxes = [r for r, (role, lab) in refs.items() if role in _TEXTBOX_ROLES and re.search(r"\b(title|name|subject)\b", lab, re.I)
                   and not re.search(r"\b(search|filter|find)", lab, re.I)]
    if nm and len(title_boxes) == 1:
        name = nm.group(1).strip(" .!?'\"“”")
        tb = title_boxes[0]
        if name and not re.match(r"(?:me|you|him|her|us|them|it)\b", name, re.I) \
                and not any(st.get("do") == "fill" and st.get("ref") == tb for st in out):
            kept = []
            for st in out:
                if (st.get("do") == "fill" and refs.get(st.get("ref"), ("",))[0] in _TEXTBOX_ROLES
                        and str(st.get("text") or "").strip().lower().startswith(name.lower())):
                    # What followed the name stays where it was typed -- but only when the request asked for
                    # contents too ("called Groceries WITH milk and eggs"); otherwise it was made up.
                    rest = str(st.get("text") or "").strip()[len(name):].lstrip(" \n:—").rstrip()
                    asked_more = bool(str(instruction or "")[nm.end():].strip(" .!?"))
                    if rest and asked_more and st.get("ref") != tb:
                        kept.append(dict(st, text=rest[:2000]))
                    st = dict(st, ref=tb, target=refs[tb][1], text=name[:200])
                kept.append(st)
            out = kept
            # Not typed anywhere at all ("a new note called Groceries with milk and eggs": the body got
            # "milk, eggs" and the title nothing) -- the name still goes in the title.
            if not any(st.get("do") == "fill" and st.get("ref") == tb for st in out):
                out.insert(0, {"do": "fill", "ref": tb, "target": refs[tb][1], "label": "Name it " + name[:30],
                               "text": name[:200], "on": False})
    # A ROW IS NOT A TEXT BOX. "email alice@x.test that the meeting moved to 3pm" came back as the text
    # "filled" onto Dana's email in the inbox list, and that row's Select box ticked (1 run in 3): a row opens
    # when clicked and has nowhere to type, so the panel would have opened Dana's email and typed into
    # whatever box was focused. Such a fill is dropped with the row's own Select tick; a request to WRITE
    # something that is then left with nothing to do starts a new one with the window's ONE Compose / New
    # message / New button -- whose form the next round fills.
    rows_typed = {near.get(st.get("ref")) for st in out if st.get("do") == "fill" and refs.get(st.get("ref"), ("",))[0] == "item"}
    if rows_typed:
        out = [st for st in out if not (st.get("do") == "fill" and refs.get(st.get("ref"), ("",))[0] == "item")
               and not (st.get("do") == "toggle" and refs.get(st.get("ref"), ("", ""))[1].lower() == "select" and near.get(st.get("ref")) in rows_typed)]
    # ...and an email to an address, answered by browsing the folders ("Open inbox", then a scroll "to find
    # alice", measured) with nothing typed anywhere, is the same request: start a new one.
    if (re.match(r"\s*(?:please\s+)?(?:e-?mail|mail|compose)\b|\s*(?:please\s+)?write\s+(?:an?\s+)?(?:new\s+)?(?:e-?mail|mail|message)\b", low)
            and not any(st.get("do") == "fill" and refs.get(st.get("ref"), ("",))[0] in _TEXTBOX_ROLES for st in out)):
        starters = [r for r, (role, lab) in refs.items() if role in ("button", "link") and re.fullmatch(r"compose|new( (message|email|mail))?", _box_key(lab))]
        if len(starters) == 1 and not any(st.get("ref") == starters[0] for st in out):
            out = [_click(starters[0])]
    # THE BUTTON THAT ALREADY RAN. In a Continue round the model pressed "New note" AGAIN beside the note it
    # had just opened (2 runs in 3) -- a second press starts a second, empty one. A press of a control the
    # previous round already pressed, when that control STARTS something (New …, Compose, Add …, Create …),
    # is dropped.
    again = {m.group(1).strip().lower() for m in (re.match(r"pressed “(.+)”$", str(x)) for x in (did or [])) if m}
    out = [st for st in out if not (st.get("do") == "click" and str(st.get("target") or "").strip().lower() in again
                                    and re.match(r"(?:\W*\s*)?(new|compose|add|create)\b", str(st.get("target") or ""), re.I))]
    # WHAT IT SHOULD SAY. "a new note called Groceries with milk and eggs" titled the note and left it empty.
    # Contents the request spells out ("with …", "saying …", "containing …") go into the one body box -- a
    # box labelled as text / body / message / content / note that is not a title, a search or optional --
    # when the plan typed nothing there.
    cm = re.search(r"\b(?:with|saying|containing|that says)\s+(.+)$", str(instruction or ""), re.I)
    if cm and any(st.get("do") == "fill" for st in out):
        bodies = [r for r, (role, lab) in refs.items() if role in _TEXTBOX_ROLES
                  and re.search(r"\b(text|body|message|content|note)\b", lab, re.I)
                  and not re.search(r"\b(title|name|subject|search|filter|find|tags?)\b|\(optional\)", lab, re.I)]
        if len(bodies) == 1 and not any(st.get("do") == "fill" and st.get("ref") == bodies[0] for st in out):
            what = cm.group(1).strip(" .!'\"“”")
            at = max(i for i, st in enumerate(out) if st.get("do") == "fill") + 1
            out.insert(at, {"do": "fill", "ref": bodies[0], "target": refs[bodies[0]][1], "label": "Write it",
                            "text": what[:2000], "on": False})
    out = _put_facts(out, refs, values, str(instruction or ""))
    # LOOKING TYPES NOTHING. "show my relay settings" once came back as a fill of the Instance box with a
    # relay URL the model made up -- run by Do all, that repoints the app at a stranger's server. A request
    # only to show / open / go somewhere, with nothing quoted and no verb that writes, has no text to type.
    # Enter in a form is SUBMITTING it ("show my relay settings": an invented URL in Instance, then Enter --
    # the URL was dropped and the Enter stayed, measured), so looking presses no Enter either. Asking to
    # extract, summarize or explain is reading the window, the same way.
    if ((_NAVIGATES.search(low) or _READS.search(low)) and not _FINDS.search(low) and not quoted
            and not _WRITES.search(low)):
        out = [st for st in out if st.get("do") != "fill" and not (st.get("do") == "press" and st.get("text") == "Enter")]
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
    # A NEW THING IS FINISHED BY ITS FORM'S OWN SUBMIT. "add Bob Smith 555-1234 to my contacts" filled the
    # New contact form and stopped (2 runs in 3) -- the button there says Save, not Add. When the request
    # makes something (add, create, new, make, schedule, save) and the plan typed into the form, the form's
    # ONE Save/Create/Add/Done/OK button ends it. Never Send: sending stays a request the person words.
    if re.search(r"\b(add|create|new|make|schedule|book|save)\b", low) and any(st.get("do") == "fill" for st in out):
        sub = [r for r, (role, lab) in refs.items() if role == "button" and _box_key(lab) in ("save", "create", "add", "done", "ok")]
        if len(sub) == 1 and not any(st.get("ref") == sub[0] and st.get("do") in ("click", "press") for st in out):
            out.append(_click(sub[0]))
    asked_to_destroy = bool(_DESTROYS.search(str(instruction or "")))
    return [st for st in out if asked_to_destroy or not _DESTROYS.search(str(st.get("target") or "") + " " + str(st.get("label") or ""))]


_TASKY = re.compile(r"\b(task|tasks|to-?do|action items?|follow[- ]?ups?|deadlines?|commitments?)\b", re.I)


async def window_steps(db, user, windows, instruction: str, history=None, commands: bool = False,
                       today: str = "", controls=None) -> dict:
    context = window_context(windows)
    out = await _chat(db, user, build_steps_messages(context, instruction, history, commands, today, controls), 0.2)
    if not out:
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    res = parse_steps(out, commands, bool(_TASKY.search(str(instruction or ""))), controls, instruction, history)
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
        # Measured on EMPTY windows: an empty drive came back as "Drake, The Weeknd… each ~15-20 times", an
        # empty calendar as six meetings. Menus, buttons and section names are not content.
        system = ("Answer the question below for the person looking at this window, from what it shows: "
                  "short, plain sentences and \"- \" bullets, the most useful thing first. If the window does "
                  "not show enough to answer, say exactly what is missing instead of guessing. Buttons, menus, "
                  "tabs and folder or section NAMES are not content: if that is all the window shows, answer in "
                  "one sentence that there is nothing here yet. Never list an item, name, number or date that "
                  "does not appear in the window text. " + _NO_INVENT + lang)
        user = _windows_block(context) + "\n\nQuestion: " + steer
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if recipe == "summary":
        # Measured in Telegram: two chats summarised as one ("meet at the trailhead for a book club").
        system = ("Summarize what this window shows, for the person looking at it: the 3 to 7 points that "
                  "matter most, each as a \"- \" bullet, saying who said or asked what. Lead with anything "
                  "that needs them (a question to answer, a deadline, a request). Keep separate conversations, "
                  "posts and items separate -- never combine facts from two of them into one point. If the "
                  "window shows only buttons and menus, say in one sentence that there is nothing to summarize. "
                  "No preamble, no closing line. " + _NO_INVENT + lang)
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


# ---- ✨ on a TIMELINE: the posts, not the page's text ---------------------------------------------------
# "Agentic window features are still kinda useless for the social timeline." They were three generic buttons
# over 4000 characters of innerText -- names, "3h", "reply repost like zap" and post text run together -- so a
# summary could not say WHICH post, nothing could be answered from it, and nothing on screen could be found.
# The client now sends the posts themselves, numbered, and every answer here names posts BY NUMBER; the client
# turns each number back into that card (jump to it, open it, reply to it). A number the model invents that
# is not on screen is dropped, and the deterministic floors below hold whatever the model misses.
FEED_RECIPES = ("digest", "needs", "reply", "find")
FEED_POSTS_MAX = 30
FEED_POST_CHARS = 600


def feed_posts(posts, limit: int = FEED_POSTS_MAX) -> list:
    """[{n, who, text, mine, to_me}] -- numbered 1..N in the order given, clipped, empties dropped."""
    out = []
    for p in list(posts or [])[:limit]:
        if not isinstance(p, dict):
            continue
        text = re.sub(r"\s+", " ", str(p.get("text") or "")).strip()[:FEED_POST_CHARS]
        if not text and not p.get("who"):
            continue
        out.append({"n": len(out) + 1, "who": _clean(p.get("who") or "someone", 60) or "someone",
                    "text": text, "mine": bool(p.get("mine")), "to_me": bool(p.get("to_me")) and not p.get("mine")})
    return out


def _feed_block(posts: list, subject: str = "posts") -> str:
    if subject == "notes":
        return ("The user's notes, numbered (title, then the start of the text):\n<<<NOTES\n"
                + "\n".join(f"[{p['n']}] {p['who']}: {p['text']}" for p in posts) + "\nNOTES")
    lines = []
    for p in posts:
        tag = " (your own post)" if p["mine"] else " (addressed to you)" if p["to_me"] else ""
        lines.append(f"[{p['n']}] {p['who']}{tag}: {p['text']}")
    return "Posts on screen, numbered:\n<<<POSTS\n" + "\n".join(lines) + "\nPOSTS"


def build_feed_messages(posts: list, recipe: str, query: str = "", target: int = 0, subject: str = "posts") -> list:
    if subject == "results":
        # WEB SEARCH RESULTS, numbered like posts: which ones answer what was searched, each with why. The
        # free-text answer this replaced named the results it liked in prose and linked to none of them.
        block = ("Search results, numbered (site: title -- snippet):\n<<<RESULTS\n"
                 + "\n".join(f"[{p['n']}] {p['who']}: {p['text']}" for p in posts) + "\nRESULTS")
        system = ("The user searched the web. Which of the numbered results actually answer the search? Reply with "
                  "ONLY a JSON object: {\"items\": [{\"n\": the result's number, \"why\": what it offers, in a few "
                  "words}]}, best first, at most 5 -- {\"items\": []} when none of them answer it. " + _NO_INVENT)
        return [{"role": "system", "content": system},
                {"role": "user", "content": block + "\n\nThe search: " + (query or "(not shown)")}]
    if subject == "notes":
        # THE SAME NUMBERED SHAPE FOR A NOTEBOOK: "Find a note about…" and "What's in my notes" answer with
        # note numbers the client turns back into notes it can open.
        if recipe == "find":
            system = ("Which of the user's notes are about what they are looking for? Match the MEANING, not only "
                      "the words. Reply with ONLY a JSON object: {\"posts\": [note numbers]}, best match first -- "
                      "{\"posts\": []} when none are. A note that is merely the closest is NOT a match: if no note "
                      "is actually about it, the answer is []. ")
            return [{"role": "system", "content": system},
                    {"role": "user", "content": _feed_block(posts, subject) + "\n\nLooking for: " + query}]
        system = ("Group the user's notes into 2 to 6 topics, the biggest first. Reply with ONLY a JSON object: "
                  "{\"topics\": [{\"title\": a few words, \"summary\": one plain sentence on what these notes "
                  "hold, \"posts\": [the note numbers in it]}]}. Every number must be one of the [n] shown. "
                  + _NO_INVENT)
        return [{"role": "system", "content": system}, {"role": "user", "content": _feed_block(posts, subject)}]
    if recipe == "digest":
        system = ("Group these social media posts into 2 to 6 topics people are talking about, the busiest first. "
                  "Reply with ONLY a JSON object: {\"topics\": [{\"title\": a few words, \"summary\": one or two "
                  "plain sentences saying who says what, \"posts\": [the numbers of the posts in it]}]}. Every "
                  "number must be one of the [n] shown. Put anything addressed to the user first. " + _NO_INVENT)
        user = _feed_block(posts)
    elif recipe == "needs":
        system = ("Which of these posts would the user want to answer? A question put to them, a reply to them, a "
                  "mention, a request or an invitation -- not ordinary news. Reply with ONLY a JSON object: "
                  "{\"items\": [{\"n\": the post's number, \"why\": a few words, e.g. \"asks you for a link\"}]}; "
                  "{\"items\": []} when nothing needs them. " + _NO_INVENT)
        user = _feed_block(posts)
    elif recipe == "find":
        system = ("Which posts are about what the user is looking for? Match the MEANING, not only the words. Reply "
                  "with ONLY a JSON object: {\"posts\": [numbers]} -- {\"posts\": []} when none are. A post that is "
                  "merely the closest is NOT a match: if no post is actually about it, the answer is [].")
        user = _feed_block(posts) + "\n\nLooking for: " + query
    else:
        post = next((p for p in posts if p["n"] == target), None)
        if not post:
            raise AssistError(400, "Pick a post to reply to.")
        # ONLY the post being answered. With the rest of the timeline beside it as "context", the model
        # answered the OTHER posts (measured: one reply to the post, one to Dana's meetup, one about the
        # kernel thread).
        system = ("Write three different replies the user could post to the post below, each in a natural "
                  "first-person voice, short (one to three sentences), and different in approach -- e.g. one "
                  "agrees or adds something, one asks a question, one is light or funny. Reply with ONLY a JSON "
                  "object: {\"replies\": [\"…\", \"…\", \"…\"]}. No hashtags unless the post uses them. "
                  + _NO_INVENT + _GAPS + " Write in the language of the post.")
        user = (f"The post to reply to, by {post['who']}:\n<<<POST\n{post['text']}\nPOST"
                + (("\n\nThe user wants: " + query) if query else ""))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _list(v) -> list:
    """A model field that should be a list, as a list: `{"topics": 5}` or `{"replies": "Great post!"}` from a model
    that slipped must not become a TypeError (a 500) or a string iterated letter by letter. A lone string is one
    item; anything else that is not a list is none (code review, 2026-10-07)."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v]
    return []


def _json_obj(out: str):
    import json
    t = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S | re.I)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _loose_fields(out: str, keys) -> dict:
    """{key: value} read one field at a time from JSON that does not parse -- the model's usual slip is an
    unquoted value ("what": tip amount per person), which costs json.loads the WHOLE answer."""
    t = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S | re.I)
    got = {}
    for k in keys:
        m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*"|[^,\n}]+)' % re.escape(k), t)
        if m:
            v = m.group(1).strip()
            got[k] = v[1:-1].replace('\\"', '"') if v.startswith('"') and v.endswith('"') and len(v) > 1 else v.strip('"')
    return got


def _nums(v, n_max: int) -> list:
    out = []
    for x in v if isinstance(v, list) else []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n_max and i not in out:
            out.append(i)
    return out


def _words(q: str) -> list:
    return [w for w in re.findall(r"[\w#@']+", str(q or "").lower()) if len(w) >= 3]


def _loose_feed(out: str) -> dict:
    """The fields read one by one from JSON that does not parse. Measured: 3 digests in 4 came back with an
    unquoted "summary" or a missing comma between topics -- the right content, and json.loads refused all of it."""
    t = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S | re.I)
    q = lambda v: v.strip().strip(",").strip().strip('"').strip()
    topics = []
    for block in re.split(r'(?=["\']title["\']\s*:)', t)[1:]:
        title = re.search(r'title["\']\s*:\s*"([^"\n]+)"', block)
        summ = re.search(r'summary["\']\s*:\s*(.+)', block)
        nums = re.search(r'posts["\']\s*:\s*\[([^\]]*)\]', block)
        if title and nums:
            topics.append({"title": title.group(1), "summary": q(summ.group(1)) if summ else "",
                           "posts": re.findall(r"\d+", nums.group(1))})
    items = [{"n": n, "why": q(w or "")} for n, w in
             re.findall(r'"n"\s*:\s*(\d+)\s*,?\s*(?:"why"\s*:\s*([^\n}]+))?', t)]
    found = re.search(r'"posts"\s*:\s*\[([^\]]*)\]', t)
    return {"topics": topics, "items": items, "posts": re.findall(r"\d+", found.group(1)) if found else []}


def parse_feed(recipe: str, out: str, posts: list, query: str = "") -> dict:
    """The model's JSON -> what the client draws. Every post number is checked against what was sent; the
    floors (posts addressed to the person for "needs", literal matches for "find") hold regardless."""
    raw = _json_obj(out) or _loose_feed(out)
    n_max = len(posts)
    if recipe == "digest":
        topics = []
        for t in _list(raw.get("topics")):
            if not isinstance(t, dict):
                continue
            nums = _nums(t.get("posts"), n_max)
            title = _clean(t.get("title"), 80)
            if nums and title:
                topics.append({"title": title, "summary": _clean(t.get("summary"), 400), "posts": nums})
        return {"topics": topics[:6]}
    if recipe == "needs":
        items, seen = [], set()
        for it in _list(raw.get("items")):
            if not isinstance(it, dict):
                continue
            nums = _nums([it.get("n")], n_max)
            if nums and nums[0] not in seen and not posts[nums[0] - 1]["mine"]:
                seen.add(nums[0])
                items.append({"n": nums[0], "why": _clean(it.get("why"), 120)})
        for p in posts:                                   # the floor: addressed to them is never left out
            if p["to_me"] and p["n"] not in seen:
                seen.add(p["n"])
                items.append({"n": p["n"], "why": "addressed to you"})
        return {"items": items}
    if recipe == "find":
        nums = _nums(raw.get("posts"), n_max)
        words = _words(query)
        if words:                                         # the floor: a post that says it, literally
            for p in posts:
                low = p["text"].lower() + " " + p["who"].lower()
                if p["n"] not in nums and all(w in low for w in words):
                    nums.append(p["n"])
        return {"posts": sorted(nums)}
    replies = [clean_recipe_text(r) for r in _list(raw.get("replies")) if isinstance(r, str)]
    if not replies:                                       # JSON that does not parse: the quoted strings in it
        m = re.search(r'"replies"\s*:\s*\[(.*)', str(out or ""), re.S)
        if m:
            replies = [clean_recipe_text(x.replace('\\"', '"').replace("\\n", "\n"))
                       for x in re.findall(r'"((?:[^"\\]|\\.){4,})"', m.group(1))]
    if not replies:                                       # prose instead of JSON: one reply is still a reply
        one = clean_recipe_text(out)
        replies = [one] if one and not one.lstrip().startswith("{") else []
    return {"replies": [r[:1000] for r in replies if r][:3]}


async def window_feed(db, user, posts, recipe: str, query: str = "", target=0, subject: str = "posts") -> dict:
    recipe = str(recipe or "")
    subject = subject if subject in ("notes", "results") else "posts"
    if (recipe not in FEED_RECIPES or (subject == "notes" and recipe not in ("digest", "find"))
            or (subject == "results" and recipe != "needs")):
        raise AssistError(400, "Unknown action.")
    posts = feed_posts(posts, 80 if subject == "notes" else FEED_POSTS_MAX)
    if not posts:
        raise AssistError(400, "There are no notes yet." if subject == "notes" else
                          "There are no results on screen — search first." if subject == "results" else
                          "There are no posts on screen yet — scroll the timeline, then try again.")
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:INSTRUCTION_MAX]
    if recipe == "find" and not query:
        raise AssistError(400, "Say what to look for.")
    try:
        target = int(target or 0)
    except (TypeError, ValueError):
        target = 0
    out = await _chat(db, user, build_feed_messages(posts, recipe, query, target, subject), 0.6 if recipe == "reply" else 0.2)
    if not out and recipe != "needs":
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    res = parse_feed(recipe, out, posts, query)
    count = len(next(iter(res.values())))
    logger.info("[chat-assist] window feed %s %s: %d in -> %d", subject, recipe, len(posts), count)
    if recipe == "reply" and not res["replies"]:
        raise AssistError(502, "The AI did not come up with a reply — try again.")
    return {"feed": {"kind": recipe, **res}}


# ---- ✨ in NOTES: the note itself, not the screen ---------------------------------------------------------
# "Agentic window features useless in notes." The panel looked for a VISIBLE text box, and a note opens
# rendered (read mode) with its textarea hidden -- so for every note that already had text the panel found
# no note at all and offered "Summarize" of the screen. The client now sends the open note's title and body
# from the notebook (PCNotes.current), and each answer is applied through the notebook's own save.
NOTE_RECIPES = ("tidy", "checklist", "continue", "title", "write")


def build_note_messages(recipe: str, title: str, body: str, ask: str = "") -> list:
    lang = " Write in the language of the note."
    if recipe in ("tidy", "checklist"):
        # Measured: the note's TITLE came back as the body's first line ("Trip Ideas" over the trip ideas),
        # and "lisbon? or porto. check flights. ask dana" became ONE checklist item.
        shape = ("as a checklist: one \"- [ ] \" line per separate thing to do -- split a sentence that holds "
                 "several tasks into several items -- grouped under short headings only when there are several "
                 "topics" if recipe == "checklist" else
                 "clean and well organised: fix spelling and grammar, put related lines together, use short "
                 "headings and \"- \" bullets where they help")
        system = ("Rewrite the user's note " + shape + ". Keep EVERY fact, number, link and name exactly; add "
                  "nothing new and drop nothing that matters. The note's title is shown separately -- do not "
                  "repeat it as a heading. Output ONLY the rewritten note." + lang)
    elif recipe == "continue":
        system = ("Continue the user's note from where it stops: write only the NEXT part, in the same voice and "
                  "format (the next paragraph, or the next few list items in the same style). Do not repeat what "
                  "is already there. Never make up facts -- prices, dates, times, names, numbers -- that the "
                  "note does not give. Output ONLY the new text." + _GAPS + lang)
    elif recipe == "title":
        system = ("Give this note a short title, 2 to 8 words, that says what it is about. Output ONLY the "
                  "title -- no quotes, no full stop." + lang)
    else:
        system = ("Write the note the user asks for, in Markdown: a first line \"# \" with a short title, then the "
                  "note -- short headings and \"- \" bullets where they help, no preamble." + _GAPS
                  + " Write in the language of the request.")
    user = ("Write a note: " + ask) if recipe == "write" else (
        f"Title: {title or '(none)'}\n<<<NOTE\n{body}\nNOTE" + (("\n\nAlso: " + ask) if ask else ""))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _drop_title_line(t: str, title: str) -> str:
    """A first or last line that only repeats the title ("# Trip ideas" over the trip ideas, or a closing
    "Title: Trip ideas" -- both measured) is not content."""
    if not title:
        return t
    same = lambda x: re.sub(r"^#+\s*|^title\s*:\s*|[*_]", "", x.strip(), flags=re.I).strip().rstrip(".:").lower() == title.strip().lower()
    lines = t.strip().split("\n")
    while lines and same(lines[0]):
        lines = lines[1:]
    while lines and same(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def parse_note(recipe: str, out: str, title: str = "", body: str = "") -> dict:
    t = clean_recipe_text(out)
    if recipe in ("tidy", "checklist"):
        t = _drop_title_line(t, title)
    if recipe == "continue":
        if re.search(r"^\s*[-*]\s+\[[ xX]\]", body, re.M):     # a checklist goes on as a checklist
            t = "\n".join(re.sub(r"^(\s*)(?:[-*]\s+)?\[\s?\]\s*", r"\1- [ ] ", x) if re.match(r"^\s*(?:[-*]\s+)?\[\s?\]", x) else x
                          for x in t.split("\n"))
            t = "\n".join(x for x in t.split("\n") if not re.match(r"^\s*- \[ \]\s*(?:\[ ?\]\s*)*$", x))
        return {"append": t.strip()}
    if recipe == "title":
        line = next((x for x in t.split("\n") if x.strip()), "")
        line = re.sub(r"^#+\s*|^title:\s*", "", line.strip(), flags=re.I).strip().strip("\"'“”*").rstrip(".")
        return {"title": line[:120]}
    if recipe == "write":
        lines = t.split("\n")
        first = next((i for i, x in enumerate(lines) if x.strip()), None)
        title = ""
        if first is not None and re.match(r"^#{1,3}\s+\S", lines[first]):
            title = re.sub(r"^#+\s*", "", lines[first]).strip()[:120]
            t = "\n".join(lines[first + 1:]).strip()
        return {"title": title, "body": t}
    if recipe == "checklist":
        t = as_checklist(t)
    return {"body": t}


async def window_note(db, user, recipe: str, title: str = "", body: str = "", ask: str = "") -> dict:
    recipe = str(recipe or "")
    if recipe not in NOTE_RECIPES:
        raise AssistError(400, "Unknown action.")
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:200]
    body = str(body or "").strip()[:RECIPE_TEXT_MAX]
    ask = re.sub(r"\s+", " ", str(ask or "")).strip()[:INSTRUCTION_MAX]
    if recipe == "write" and not ask:
        raise AssistError(400, "Say what the note should be about.")
    if recipe != "write" and not body:
        raise AssistError(400, "This note is empty — write something first, or use “Write it for me”.")
    out = await _chat(db, user, build_note_messages(recipe, title, body, ask), 0.5 if recipe in ("write", "continue") else 0.2)
    res = parse_note(recipe, out, title, body)
    if not any(res.values()):
        raise AssistError(502, "The AI did not come up with an answer — try again.")
    logger.info("[chat-assist] window note %s: %d chars in -> %d out", recipe, len(body) + len(ask),
                sum(len(v) for v in res.values()))
    return {"note": {"kind": recipe, **res}}


# ---- ✨ in CONTACTS: "Add a contact…" -> the New contact form, filled in -----------------------------------
# The free-form agent opened "+ Contact" and then had to plan a form it had not seen (measured 0/3). One
# sentence in, one validated set of fields out, and the client opens the app's own form with them for the
# person to check and Save. A phone number or an email address in what they typed is ALWAYS carried over,
# whatever the model returned -- those are copied, never generated.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d ().-]{5,}\d(?![\w])")


def parse_contact(out: str, said: str) -> dict:
    raw = _json_obj(out) or _loose_fields(out, ("given", "family", "phone", "email", "org", "note"))
    c = {k: _clean(raw.get(k), 120) for k in ("given", "family", "phone", "email", "org", "note")}
    em, ph = _EMAIL.search(said or ""), _PHONE.search(said or "")
    c["email"] = em.group(0) if em else ""            # only ever what the person typed
    c["phone"] = ph.group(0).strip() if ph else ""
    if not (c["given"] or c["family"]):
        rest = _PHONE.sub(" ", _EMAIL.sub(" ", said or ""))
        words = [w for w in re.findall(r"[^\W\d_][\w'-]*", rest) if w.lower() not in ("add", "contact", "new", "phone", "email", "at", "is", "and")]
        if words:
            c["given"], c["family"] = words[0], " ".join(words[1:2])
    return c


async def window_contact(db, user, said: str) -> dict:
    said = re.sub(r"\s+", " ", str(said or "")).strip()[:INSTRUCTION_MAX]
    if not said:
        raise AssistError(400, "Say who to add — a name, and a phone number or email if you have one.")
    msgs = [{"role": "system", "content": (
        "Turn the user's words into one address-book contact. Reply with ONLY a JSON object: {\"given\": first "
        "name, \"family\": last name, \"phone\": \"\", \"email\": \"\", \"org\": company or \"\", \"note\": "
        "anything else they said about the person, or \"\"}. Use only what they wrote; \"\" for anything missing.")},
        {"role": "user", "content": said}]
    out = await _chat(db, user, msgs, 0.1)
    c = parse_contact(out, said)
    logger.info("[chat-assist] window contact: %d chars -> %d fields", len(said), sum(1 for v in c.values() if v))
    return {"contact": c}


# ---- ✨ in the CALCULATOR: words -> an expression the calculator itself evaluates ---------------------------
# The free-form agent pressed keypad buttons and got "× 7" (measured 0/3). A model is bad at arithmetic and
# fine at translation, so it only TRANSLATES ("15% tip on 84.50" -> "84.50*15/100"); the client's own
# evaluator does the maths, and nothing but the calculator's own symbols can come back.
_CALC_OK = re.compile(r"^(?:[0-9.+\-*/^%()!, ]|sqrt|sin|cos|tan|ln|log|π|pi|e)+$")


def parse_calc(out: str) -> dict:
    raw = _json_obj(out) or _loose_fields(out, ("expression", "what"))
    expr = str(raw.get("expression") or "").strip()
    expr = expr.replace("×", "*").replace("÷", "/").replace("−", "-").replace("**", "^").replace("pi", "π")
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr)                  # 1,250 -> 1250
    if not expr or len(expr) > 200 or not _CALC_OK.match(expr):
        return {"expression": "", "what": ""}
    return {"expression": expr, "what": _clean(raw.get("what"), 160)}


async def window_calc(db, user, said: str) -> dict:
    said = re.sub(r"\s+", " ", str(said or "")).strip()[:INSTRUCTION_MAX]
    if not said:
        raise AssistError(400, "Say what to work out.")
    msgs = [{"role": "system", "content": (
        "Turn the user's question into ONE arithmetic expression a calculator can evaluate, using only digits, "
        ". + - * / ^ ( ) and sqrt( sin( cos( tan( ln( log( -- write a percentage as /100. Do NOT compute the "
        "answer. Reply with ONLY a JSON object: {\"expression\": \"...\", \"what\": a few words saying what it "
        "works out}.")}, {"role": "user", "content": said}]
    out = await _chat(db, user, msgs, 0.0)
    c = parse_calc(out)
    if not c["expression"]:
        raise AssistError(422, "That did not turn into a calculation — try saying it with the numbers in it.")
    logger.info("[chat-assist] window calc: %d chars -> %d char expression", len(said), len(c["expression"]))
    return {"calc": c}


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
