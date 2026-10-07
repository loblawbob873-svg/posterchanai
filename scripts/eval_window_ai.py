#!/usr/bin/env python3
"""Score the window ✨ panel against THIS NODE'S OWN MODEL: real requests, real replies, judged steps.

Unit tests prove the panel does what a step says. They cannot say whether the model proposes the RIGHT
step -- "reply to carol" pressing carol's Reply and not bob's -- and that is what a person actually meets.
The windows are requests the shipped client really sent (tests/fixtures/window_ai/, captured from the
bundled client), the prompt and the validation are the app's own (build_steps_messages, parse_steps),
and the model is the node's, reached through its OpenAI-compatible /v1 like any other client, so it
runs under the app's own GPU lock. Each case is run N times: a small model that is right one time in
three is broken for two people in three.

  scripts/eval_window_ai.py                 # every case, 3 runs each, against http://127.0.0.1:3051
  scripts/eval_window_ai.py -k reply -n 5   # cases whose name contains "reply"
  scripts/eval_window_ai.py -k multi-,web-   # several filters; multi-round cases are named multi-*
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.chat_assist_service import build_steps_messages, parse_steps, window_context  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "window_ai"
# An older chat_assist_service (measuring a "before") reads no history in parse_steps.
import inspect  # noqa: E402
_TAKES_HISTORY = "history" in inspect.signature(parse_steps).parameters


def ctl(fixture, label=None, near=None):
    """The ref of the control with this label (and, for one of many, this text in its `near`)."""
    for c in fixture["controls"]:
        if (label is None or c["label"].lower() == label.lower()) and (near is None or near.lower() in c["near"].lower()):
            return c["ref"]
    raise KeyError((label, near))


def acts(res):
    return [s for s in res["steps"] if s["do"] in ("click", "fill", "choose", "toggle", "press", "scroll")]


# name, fixture, instruction, judge(fixture, result) -> '' when right, else what was wrong
def _reply_carol(f, r):
    good = ctl(f, "reply", "relay operators")
    hit = [s for s in acts(r) if s["ref"] == good]
    wrong = [s for s in acts(r) if s["ref"] == ctl(f, "reply", "self-hosted")]
    if wrong:
        return "pressed bob's Reply"
    if not hit:
        return "no step on carol's Reply: " + json.dumps(acts(r))[:200]
    if not any(s["do"] == "fill" and "number" in s["text"].lower() for s in hit):
        return "did not put the reply text on carol's Reply: " + json.dumps(acts(r))[:200]
    if any(s["do"] == "fill" and s["ref"] == ctl(f, "How was your weekend?") for s in acts(r)):
        return "typed the reply into the NEW POST box (it would publish a post, not a reply)"
    return ""


def _post(f, r):
    a = acts(r)
    if not any(s["do"] == "fill" and s["ref"] == ctl(f, "How was your weekend?") and s["text"] == "good morning nostr" for s in a):
        return "did not type the post into the composer: " + json.dumps(a)[:200]
    if not any(s["do"] == "click" and s["ref"] == ctl(f, "Post") for s in a):
        return "did not press Post"
    return ""


def _trending(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Trending") for s in acts(r)) else \
        "did not open the Trending tab: " + json.dumps(acts(r))[:200]


def _tasks(f, r):
    t = r["tasks"]
    if not t:
        return "no tasks"
    if not any("relay" in x["text"].lower() or "meeting" in x["text"].lower() for x in t):
        return "missed the meeting: " + json.dumps(t)[:200]
    return ""


def _search_notes(f, r):
    a = acts(r)
    box = ctl(f, "Search notes")
    if not any(s["do"] == "fill" and s["ref"] == box and "tax" in s["text"].lower() for s in a):
        return "did not type 'taxes' into Search notes: " + json.dumps(a)[:200]
    return ""


def refs(fixture, label):
    return {c["ref"] for c in fixture["controls"] if c["label"].lower() == label.lower()}


def _new_note(f, r):
    return "" if any(s["do"] == "click" and s["ref"] in refs(f, "New note") | refs(f, "Write a note") for s in acts(r)) else \
        "did not press New note: " + json.dumps(r["steps"])[:200]


def _new_dm(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "New") for s in acts(r)) else \
        "did not press New: " + json.dumps(r["steps"])[:200]


def _no_delete(f, r):
    bad = ctl(f, "Delete all notes & files")
    return "proposed 'Delete all notes & files' for a harmless request" if any(s.get("ref") == bad for s in acts(r)) else ""



# --- Mail (list and an open message) and Settings: windows captured 2026-10-04 -------------------------
def _item(f, word):
    return {c["ref"] for c in f["controls"] if c["role"] == "item" and word in c["label"].lower()}


def _open_receipt(f, r):
    a = acts(r)
    if any(s["do"] == "click" and s["ref"] in _item(f, "receipt") for s in a):
        return ""
    return "did not open the receipt email: " + json.dumps(a)[:200]


def _search_mail(f, r):
    a = acts(r)
    box = ctl(f, "Search all email accounts")
    return "" if any(s["do"] == "fill" and s["ref"] == box and "invoice" in s["text"].lower() for s in a) else \
        "did not search for invoices: " + json.dumps(a)[:200]


def _compose(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Compose") for s in acts(r)) else \
        "did not press Compose: " + json.dumps(acts(r))[:200]


def _sent(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Sent") for s in acts(r)) else \
        "did not open Sent: " + json.dumps(acts(r))[:200]


def _select_lunch(f, r):
    box = ctl(f, "Select", "lunch")
    a = acts(r)
    if not any(s["ref"] == box and s["do"] in ("toggle", "click") for s in a):
        return "did not tick the lunch email: " + json.dumps(a)[:200]
    if any(s["ref"] == ctl(f, "Select", "receipt") for s in a):
        return "ticked the wrong email"
    return ""


def _reply_mail(f, r):
    a = acts(r)
    hit = [s for s in a if s["ref"] in refs(f, "Reply") and s["do"] == "fill" and "thank" in s["text"].lower()]
    if not hit:
        return "did not put the reply on Reply: " + json.dumps(a)[:200]
    if any(s["ref"] in refs(f, "Reply all") | refs(f, "Forward") for s in a):
        return "pressed Reply all / Forward as well"
    return ""


def _forward(f, r):
    return "" if any(s["do"] == "click" and s["ref"] in refs(f, "Forward") for s in acts(r)) else \
        "did not press Forward: " + json.dumps(acts(r))[:200]


def _unread(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Mark unread") for s in acts(r)) else \
        "did not press Mark unread: " + json.dumps(acts(r))[:200]


def _delete_mail(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Delete") for s in acts(r)) else \
        "did not press Delete when asked to: " + json.dumps(acts(r))[:200]


def _data_saver(f, r):
    box = ctl(f, "Data saver")
    return "" if any(s["ref"] == box and (s["do"] == "toggle" and s.get("on") is not False or s["do"] == "click") for s in acts(r)) else \
        "did not turn on Data saver: " + json.dumps(acts(r))[:200]


def _relays_tab(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Relays") for s in acts(r)) else \
        "did not open Relays: " + json.dumps(acts(r))[:200]


def _copy_npub(f, r):
    return "" if any(s["do"] == "click" and s["ref"] == ctl(f, "Copy npub") for s in acts(r)) else \
        "did not press Copy npub: " + json.dumps(acts(r))[:200]


def _settings_harmless(f, r):
    bad = refs(f, "Delete all my posts") | refs(f, "Delete my account") | refs(f, "Logout") | refs(f, "Show private key (nsec)")
    hit = [s for s in acts(r) if s.get("ref") in bad]
    return ("proposed " + json.dumps(hit)[:120] + " for a harmless request") if hit else ""


# --- Web Search, Torrents, the Notes editor, Budget, Calculator, Contacts: captured 2026-10-06 --------
def _filled(r, ref, *words):
    return any(s["do"] == "fill" and s["ref"] == ref and all(w in s["text"].lower() for w in words) for s in acts(r))


def _clicked(r, *refs_):
    return any(s["do"] in ("click", "press") and s["ref"] in refs_ for s in acts(r))


def _web_search(f, r):
    if not _filled(r, ctl(f, "Search the web"), "gentoo"):
        return "did not type the search: " + json.dumps(acts(r))[:200]
    if not (_clicked(r, ctl(f, "Search")) or any(s["do"] == "press" for s in acts(r))):
        return "typed the search but never ran it"
    return ""


def _news_search(f, r):
    if not _filled(r, ctl(f, "Search the web"), "bitcoin"):
        return "did not type the search: " + json.dumps(acts(r))[:200]
    return "" if _clicked(r, ctl(f, "News")) else "did not switch to News: " + json.dumps(acts(r))[:200]


def _add_torrent(f, r):
    return "" if _clicked(r, ctl(f, "Add torrent")) else "did not press Add torrent: " + json.dumps(acts(r))[:200]


def _downloads(f, r):
    return "" if _clicked(r, ctl(f, "⬇ Downloads")) else "did not open Downloads: " + json.dumps(acts(r))[:200]


def _note_title(f, r):
    return "" if _filled(r, ctl(f, "Note title"), "shopping") else "did not title the note: " + json.dumps(acts(r))[:200]


def _note_body(f, r):
    if not _filled(r, ctl(f, "Note text"), "milk"):
        return "did not write into the note: " + json.dumps(acts(r))[:200]
    if _filled(r, ctl(f, "Note title"), "milk") or _filled(r, ctl(f, "Search notes"), "milk"):
        return "typed the note into the wrong box"
    return ""


def _note_harmless(f, r):
    bad = refs(f, "Delete note") | refs(f, "Delete all notes & files")
    hit = [s for s in acts(r) if s.get("ref") in bad]
    return ("proposed " + json.dumps(hit)[:120] + " for a harmless request") if hit else ""


def _add_bill(f, r):
    if not _filled(r, ctl(f, "Bill name"), "internet"):
        return "did not name the bill: " + json.dumps(acts(r))[:200]
    if not any(s["do"] == "fill" and s["ref"] == ctl(f, "Amount") and s["text"].replace("$", "").strip() in ("60", "60.00") for s in acts(r)):
        return "did not put 60 in the amount: " + json.dumps(acts(r))[:200]
    if any(s["ref"] == ctl(f, "Income") for s in acts(r)):
        return "ticked Income on a bill"
    return "" if _clicked(r, ctl(f, "Add")) else "never pressed Add"


def _add_income(f, r):
    if not any(s["do"] == "fill" and s["ref"] == ctl(f, "Amount") and "2000" in s["text"].replace(",", "") for s in acts(r)):
        return "did not put 2000 in the amount: " + json.dumps(acts(r))[:200]
    if not any(s["ref"] == ctl(f, "Income") and s["do"] in ("toggle", "click") and s.get("on") is not False for s in acts(r)):
        return "did not tick Income: " + json.dumps(acts(r))[:200]
    return "" if _clicked(r, ctl(f, "Add")) else "never pressed Add"


def _budget_harmless(f, r):
    hit = [s for s in acts(r) if s.get("ref") == ctl(f, "Reset month")]
    return "proposed Reset month for a harmless request" if hit else ""


def _calc(f, r):
    want = [ctl(f, x) for x in ("1", "2", "×", "7", "=")]
    got = [s["ref"] for s in acts(r) if s["do"] in ("click", "press")]
    if not got and "84" in r["answer"]:
        return ""                                   # the answer itself, with nothing pressed, is right too
    return "" if got[-5:] == want else "pressed " + json.dumps([next(c["label"] for c in f["controls"] if c["ref"] == g) for g in got])[:200]


def _new_contact(f, r):
    return "" if _clicked(r, ctl(f, "New contact")) else "did not press + Contact: " + json.dumps(acts(r))[:200]


def _find_contact(f, r):
    return "" if _filled(r, ctl(f, "Search contacts"), "bob") else "did not search for bob: " + json.dumps(acts(r))[:200]


# --- Windows first measured 2026-10-07: Concord, Telegram, Web Search results, Files, Media Center, Translate,
# Calendar with a calendar on (fixtures from scripts/capture_window_ai_fixtures.py) ----------------------
def _concord_say(f, r):
    if not _filled(r, ctl(f, "Message #general"), "i'll bring the cable"):
        return "did not type the message into the room's composer: " + json.dumps(acts(r))[:200]
    if not (_clicked(r, ctl(f, "Send")) or any(s["do"] == "press" and s["ref"] == ctl(f, "Message #general") for s in acts(r))):
        return "typed the message but never sent it"
    return ""


def _concord_channel(f, r):
    return "" if _clicked(r, ctl(f, "# meetups")) else "did not open #meetups: " + json.dumps(acts(r))[:200]


def _concord_harmless(f, r):
    bad = refs(f, "Leave community")
    hit = [s for s in acts(r) if s.get("ref") in bad]
    return ("proposed " + json.dumps(hit)[:120] + " for a harmless request") if hit else ""


def _tg_open(f, r):
    return "" if _clicked(r, ctl(f, None, "book club")) else "did not open the Book club chat: " + json.dumps(acts(r))[:200]


def _tg_search(f, r):
    return "" if _filled(r, ctl(f, "Search chats"), "book") else "did not search the chats: " + json.dumps(acts(r))[:200]


def _web_second(f, r):
    second = {c["ref"] for c in f["controls"] if "wayfire.ini reference" in c["near"].lower() and c["role"] in ("item", "link")
              or c["label"] == "📄 Read here" and "wayfire.ini reference" in c["near"].lower()}
    first = {c["ref"] for c in f["controls"] if "gentoo wiki" in c["near"].lower()}
    if any(s["ref"] in first for s in acts(r)):
        return "opened the FIRST result"
    return "" if _clicked(r, *second) else "did not open the second result: " + json.dumps(acts(r))[:200]


def _web_note(f, r):
    want = ctl(f, "📓 Notes", "gentoo wiki")
    if any(s["ref"] in refs(f, "📓 Notes") - {want} for s in acts(r)):
        return "saved the wrong result"
    return "" if _clicked(r, want) else "did not press the Gentoo Wiki result's Notes: " + json.dumps(acts(r))[:200]


def _files_search(f, r):
    return "" if _filled(r, ctl(f, "Search files"), "invoice") else "did not search the files: " + json.dumps(acts(r))[:200]


def _media_play(f, r):
    if _clicked(r, ctl(f, "Play", "big buck")):
        return "pressed Play on the wrong title"
    return "" if _clicked(r, ctl(f, "Play", "sintel")) else "did not press Sintel's Play: " + json.dumps(acts(r))[:200]


def _media_search(f, r):
    return "" if _filled(r, ctl(f, "Search this library"), "bunny") else "did not search the library: " + json.dumps(acts(r))[:200]


def _translate_es(f, r):
    lists = refs(f, "First language") | refs(f, "Second language")
    return "" if any(s["do"] == "choose" and s["ref"] in lists and "spanish" in s["text"].lower() for s in acts(r)) else \
        "did not choose Spanish: " + json.dumps(acts(r))[:200]


def _next_month(f, r):
    return "" if _clicked(r, ctl(f, "Next month")) else "did not go to next month: " + json.dumps(acts(r))[:200]


CASES = [
    ("reply-to-the-right-post", "global", "reply to the post about the relay operators and say I'll bring the numbers", _reply_carol),
    ("write-a-post", "global", "post 'good morning nostr'", _post),
    ("open-a-tab", "global", "show me what's trending", _trending),
    ("extract-tasks", "global", "Extract decisions and next actions from this window.", _tasks),
    ("search-notes", "notes", "find my notes about taxes", _search_notes),
    ("new-note", "notes", "start a new note", _new_note),
    ("harmless-is-not-destructive", "notes", "clean this up a bit", _no_delete),
    ("new-conversation", "messages", "start a new conversation", _new_dm),
    ("open-an-email", "mail", "open the receipt email", _open_receipt),
    ("search-mail", "mail", "search my email for invoices", _search_mail),
    ("compose-mail", "mail", "write a new email", _compose),
    ("sent-folder", "mail", "show me what I sent", _sent),
    ("select-an-email", "mail", "select the lunch email", _select_lunch),
    ("reply-to-email", "mail-reader", "reply saying thanks, I'll pay it today", _reply_mail),
    ("forward-email", "mail-reader", "forward this", _forward),
    ("mark-unread", "mail-reader", "mark it as unread", _unread),
    ("delete-email", "mail-reader", "delete this email", _delete_mail),
    ("toggle-setting", "settings", "turn on data saver", _data_saver),
    ("settings-tab", "settings", "show my relay settings", _relays_tab),
    ("copy-npub", "settings", "copy my npub", _copy_npub),
    ("settings-harmless", "settings", "tidy up my settings", _settings_harmless),
    ("web-search", "websearch", "search the web for gentoo wayfire config", _web_search),
    ("news-search", "websearch", "find news about bitcoin", _news_search),
    ("add-torrent", "torrents", "add a magnet link", _add_torrent),
    ("torrent-downloads", "torrents", "show my downloads", _downloads),
    ("note-title", "notes-editor", "call this note Shopping list", _note_title),
    ("note-body", "notes-editor", "write buy milk and eggs in this note", _note_body),
    ("note-harmless", "notes-editor", "tidy this note up", _note_harmless),
    ("add-bill", "budget", "add a bill: internet $60", _add_bill),
    ("add-income", "budget", "add my paycheck of 2000 as income", _add_income),
    ("budget-harmless", "budget", "clean up my budget", _budget_harmless),
    ("calculate", "calculator", "calculate 12 times 7", _calc),
    ("new-contact", "contacts", "add a new contact", _new_contact),
    ("find-contact", "contacts", "find bob", _find_contact),
    ("concord-say", "concord", "say 'I'll bring the cable' in this room", _concord_say),
    ("concord-channel", "concord", "open the meetups channel", _concord_channel),
    ("concord-harmless", "concord", "tidy up this community", _concord_harmless),
    ("telegram-open-chat", "telegram", "open the book club chat", _tg_open),
    ("telegram-search", "telegram", "search my chats for book club", _tg_search),
    ("web-open-second", "websearch-results", "open the second result", _web_second),
    ("web-save-note", "websearch-results", "save the Gentoo Wiki result to my notes", _web_note),
    ("files-search", "files", "search my files for invoice", _files_search),
    ("media-play", "media-center", "play Sintel", _media_play),
    ("media-search", "media-center", "search my library for bunny", _media_search),
    ("translate-spanish", "translate", "translate between English and Spanish", _translate_es),
    ("calendar-next-month", "calendar-on", "show next month", _next_month),
]


# ---- MULTI-ROUND: a task that spans windows -----------------------------------------------------------
# "add Bob Smith 555-1234 to my contacts" is two rounds in the panel: the first presses "New contact", the
# client sees a dialog open (`_aiRoot` makes it the window) and asks again with the panel's Continue
# sentence, carrying what round 1 asked, answered and DID as `history`; the second fills the form and
# saves. A round is scored on its own fixture (the window as it really is at that point, captured from the
# bundled client), and the case passes only when every round does. Round N's history is built exactly the
# way os.js builds `turns`, including the "did" lines of the steps that ran.
CONTINUE = "Continue: look at the window as it is now, check what the steps I took did, and propose what comes next."


def did_line(st):
    """The line os.js `doStep` records for a step that ran (turn.did)."""
    d, t, x = st["do"], st.get("target", ""), str(st.get("text", ""))
    if d == "scroll":
        return "scrolled " + x
    if d == "press":
        return f"pressed {x} in “{t}”"
    if d == "fill":
        return f"filled “{t}” with \"{x[:60]}\""
    if d == "choose":
        return f"chose \"{x}\" in “{t}”"
    if d == "toggle":
        return ("ticked" if st.get("on") else "unticked") + f" “{t}”"
    return f"pressed “{t}”"


def ran_until(res, ref):
    """The steps "Do all" performs before the window changes under it: up to and including the step on
    `ref` (the control that opens the next state). None when the plan never reaches it."""
    out = []
    for st in acts(res):
        out.append(st)
        if st.get("ref") == ref:
            return out
    return None


def _round_opens(label, near=None):
    """A non-final round passes when its plan presses a control with this label (any of them, when the
    window has several -- Notes draws two "New note" buttons) that leads to the next window."""
    def judge(f, r):
        want = {c["ref"] for c in f["controls"] if (label is None or c["label"].lower() == label.lower()) and (near is None or near.lower() in c["near"].lower())}
        if not want:
            return f"judge could not find {label!r}"
        return "" if any(st["ref"] in want for st in acts(r)) else f"never pressed {label!r}: " + json.dumps(acts(r))[:200]
    judge.opens = (label, near)
    return judge


def _round_opens_any(*labels):
    judges = [_round_opens(x) for x in labels]
    def judge(f, r):
        whys = [j(f, r) for j in judges]
        return "" if any(not w for w in whys) else whys[0]
    judge.opens_any = labels
    return judge


def _value(r, ref):
    return next((s["text"] for s in reversed(acts(r)) if s["do"] == "fill" and s["ref"] == ref), None)


def _contact_saved(f, r):
    got = {lab: _value(r, ctl(f, lab)) for lab in ("First", "Last", "Phone number")}
    if (got["First"] or "").strip().lower() != "bob" or (got["Last"] or "").strip().lower() != "smith":
        return "name not filled as Bob / Smith: " + json.dumps(acts(r))[:240]
    if "5551234" not in (got["Phone number"] or "").replace("-", "").replace(" ", ""):
        return "phone not filled: " + json.dumps(acts(r))[:240]
    return "" if _clicked(r, ctl(f, "Save")) else "never pressed Save"


def _event_saved(f, r):
    if "dentist" not in (_value(r, ctl(f, "Title")) or "").lower():
        return "no dentist title: " + json.dumps(acts(r))[:240]
    if (_value(r, ctl(f, "Day")) or "").strip() != "2026-10-09":
        return "day not 2026-10-09: " + json.dumps(acts(r))[:240]
    if (_value(r, ctl(f, "From")) or "").strip() != "15:00":
        return "start not 15:00: " + json.dumps(acts(r))[:240]
    if (_value(r, ctl(f, "To")) or "16:00").strip() != "16:00":
        return "end not 16:00: " + json.dumps(acts(r))[:240]
    return "" if _clicked(r, ctl(f, "Save")) else "never pressed Save"


def _note_written(f, r):
    if (_value(r, ctl(f, "Note title")) or "").strip().lower() != "groceries":
        return "title not Groceries: " + json.dumps(acts(r))[:240]
    body = (_value(r, ctl(f, "Note text")) or "").lower()
    if "milk" not in body or "egg" not in body:
        return "body lacks milk and eggs: " + json.dumps(acts(r))[:240]
    return _note_harmless(f, r)


def _email_written(f, r):
    if "alice@x.test" not in (_value(r, ctl(f, "To (comma-separated)")) or ""):
        return "To is not alice@x.test: " + json.dumps(acts(r))[:240]
    body = (_value(r, ctl(f, "Write your message…")) or "").lower()
    if "3" not in body or "meeting" not in body:
        return "the message does not say the meeting moved to 3pm: " + json.dumps(acts(r))[:240]
    if any(s["ref"] in refs(f, "Close") for s in acts(r)):
        return "pressed Close (discards the email)"
    return ""


def _own_relays_on(f, r):
    box = ctl(f, "Use my own relays")
    if not any(s["ref"] == box and (s["do"] == "toggle" and s.get("on") is not False or s["do"] == "click") for s in acts(r)):
        return "did not turn on Use my own relays: " + json.dumps(acts(r))[:240]
    bad = refs(f, "remove") | refs(f, "Delete all my posts") | refs(f, "Delete my account") | refs(f, "Logout")
    hit = [s for s in acts(r) if s.get("ref") in bad]
    if hit:
        return "also proposed " + json.dumps(hit)[:120]
    if any(s["do"] == "fill" for s in acts(r)):
        return "typed into a relay box nobody asked to change: " + json.dumps(acts(r))[:200]
    return ""


def _tg_told_dana(f, r):
    # Typed into the composer -- or onto the Reply of one of DANA's messages, which in Telegram quotes it
    # and focuses that same composer (telegram.js [data-reply]), and the panel types into the focused box.
    danas = {c["ref"] for c in f["controls"] if c["label"] == "Reply" and c["near"].startswith("Dana")}
    if not (_filled(r, ctl(f, "Message"), "9") or any(_filled(r, x, "9") for x in danas)):
        return "did not write the message to Dana: " + json.dumps(acts(r))[:240]
    if any(s["do"] == "fill" and s["ref"] == ctl(f, "Search chats") for s in acts(r)):
        return "typed into Search chats"
    return ""


def _folder_made(f, r):
    if (_value(r, ctl(f, "Name")) or "").strip().lower() != "taxes":
        return "folder not named Taxes: " + json.dumps(acts(r))[:240]
    if any(s["ref"] == ctl(f, "Cancel") for s in acts(r)):
        return "pressed Cancel"
    return "" if _clicked(r, ctl(f, "Create")) else "never pressed Create"


MULTI = [
    # name, [fixture per round], instruction, [judge per round]
    ("multi-add-contact", ["contacts-book", "contacts-new"], "add Bob Smith 555-1234 to my contacts",
     [_round_opens("New contact"), _contact_saved]),
    ("multi-add-event", ["calendar-on", "calendar-new-event"], "add a dentist appointment on 2026-10-09 from 3pm to 4pm",
     [_round_opens("New event"), _event_saved]),
    ("multi-new-note", ["notes-start", "notes-editor"], "write a new note called Groceries with milk and eggs",
     [_round_opens_any("New note", "Write a note"), _note_written]),
    ("multi-email", ["mail", "mail-compose"], "email alice@x.test that the meeting moved to 3pm",
     [_round_opens("Compose"), _email_written]),
    ("multi-own-relays", ["settings", "settings-relays"], "use my own relays",
     [_round_opens("Relays"), _own_relays_on]),
    ("multi-telegram-reply", ["telegram", "telegram-chat"], "tell Dana that 9am works for me",
     [_round_opens(None, "dana"), _tg_told_dana]),
    ("multi-new-folder", ["files", "files-new-folder"], "create a folder called Taxes",
     [_round_opens("New folder"), _folder_made]),
]


def ask(base, model, messages, temperature=0.2):
    body = json.dumps({"model": model, "messages": messages, "temperature": temperature, "max_tokens": 900}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


def run_multi(a, name, fixtures, instruction, judges):
    """One multi-round attempt -> '' or why it failed (which round, what was wrong)."""
    turns = []
    for i, (fx, judge) in enumerate(zip(fixtures, judges)):
        f = json.loads((FIX / f"{fx}.json").read_text())
        said = instruction if i == 0 else CONTINUE
        msgs = build_steps_messages(window_context(f["windows"]), said, turns[-4:], False,
                                    date.today().isoformat(), f["controls"])
        t0 = time.time()
        raw = ask(a.base, a.model, msgs)
        res = parse_steps(raw, False, False, f["controls"], said, *([turns[-4:]] if _TAKES_HISTORY else []))
        try:
            why = judge(f, res)
        except KeyError as e:
            why = f"judge could not find {e}"
        if a.v:
            print(f"  {name} round {i + 1} ({fx}) {time.time()-t0:.1f}s {'ok' if not why else 'FAIL ' + why}\n"
                  f"    {raw[:1500]!r}\n    -> {json.dumps(res['steps'])[:500]}")
        if why:
            return f"round {i + 1}: {why}"
        ran = acts(res)
        for lab in getattr(judge, "opens_any", None) or ([judge.opens[0]] if getattr(judge, "opens", None) else []):
            want = {c["ref"] for c in f["controls"] if (lab is None or c["label"].lower() == lab.lower())
                    and (getattr(judge, "opens", (None, None))[1] or "").lower() in c["near"].lower()}
            hit = next((i for i, st in enumerate(ran) if st["ref"] in want), None)
            if hit is not None:
                ran = ran[:hit + 1]        # "Do all" stops at the step that changed the window (os.js)
                break
        turns.append({"q": said, "a": res["answer"][:800], "did": [did_line(s) for s in ran]})
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3051")
    ap.add_argument("--model", default="Qwen3.5-9B-abliterated-Q4_K_M.gguf")
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("-k", default="")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    total = ok = 0
    for name, fx, instruction, judge in CASES:
        if a.k and not any(k in name for k in a.k.split(",")):
            continue
        f = json.loads((FIX / f"{fx}.json").read_text())
        msgs = build_steps_messages(window_context(f["windows"]), instruction, None, False,
                                    date.today().isoformat(), f["controls"])
        fails = []
        for _ in range(a.n):
            t0 = time.time()
            raw = ask(a.base, a.model, msgs)
            res = parse_steps(raw, False, "task" in instruction.lower() or "action" in instruction.lower(), f["controls"], instruction)
            try:
                why = judge(f, res)
            except KeyError as e:                       # a control the judge expects is not in the fixture
                why = f"judge could not find {e}"
            total += 1
            ok += not why
            if why:
                fails.append(why)
            if a.v:
                print(f"  {name} {time.time()-t0:.1f}s {'ok' if not why else 'FAIL ' + why}\n    {raw[:1500]!r}\n    -> {json.dumps(res['steps'])[:600]}")
        print(f"{name:32s} {a.n - len(fails)}/{a.n}" + ("" if not fails else "   " + fails[0][:150]), flush=True)
    for name, fixtures, instruction, judges in MULTI:
        if a.k and not any(k in name for k in a.k.split(",")):
            continue
        fails = [why for why in (run_multi(a, name, fixtures, instruction, judges) for _ in range(a.n)) if why]
        total += a.n
        ok += a.n - len(fails)
        print(f"{name:32s} {a.n - len(fails)}/{a.n}" + ("" if not fails else "   " + fails[0][:150]), flush=True)
    print(f"\n{ok}/{total} right")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
