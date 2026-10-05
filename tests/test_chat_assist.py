"""✨ in Telegram and DMs — Generate reply, Summarize chat, Summarize YouTube & links.

Asked for: "AI replies to Telegram and DM's also" and "For Telegram, maybe the AI sparkle button should
have a Generate Reply Summarize youtube and links". /api/chat-assist is RUN with the model and the page
fetcher stubbed. Pinned:

  * reply: several drafts in ONE model call, drafted for the medium asked (a Telegram message, a DM),
    with the Texts never-invent rules; the Texts prompt itself is byte-for-byte unchanged;
  * summarize: a BOUNDED tail (newest kept), speakers named in a group, the user is "Me";
  * links: the NEWEST links first, at most three, each read through search_service.fetch_url_content
    (the SSRF-guarded fetcher that also turns YouTube into its transcript); an unreadable link is a
    sentence, not an invented summary; no links is a sentence;
  * the AI gate refuses before any model call; nothing logs what anybody wrote.
"""
import asyncio
import json
import logging
import os
from unittest import mock

import pytest

os.environ.setdefault("POSTERCHANAI_SKIP_DB", "1")

from app.routers import chat_assist as R  # noqa: E402
from app.services import chat_assist_service as svc  # noqa: E402
from app.services import texts_ai_service as texts  # noqa: E402

SECRET = "the gate code is 4471"


class _Chat:
    def __init__(self, answers):
        self.answers, self.calls, self.temperature = list(answers), [], 0.7

    async def chat(self, msgs):
        self.calls.append(msgs)
        return self.answers.pop(0) if self.answers else ""


class _User:
    is_admin = False
    can_ai = True
    nostr_npub = ""


def _run(body, chat, user=_User(), allowed=True, pages=None):
    class _CS:
        def __init__(self, db, user=None):
            self.chat_service = chat

    async def ai_allowed(u):
        return allowed

    fetched = []

    class _Search:
        async def fetch_url_content(self, url, max_length=0):
            fetched.append(url)
            return (pages or {}).get(url) or {"url": url, "title": url, "content": "", "error": "URL blocked: private address"}

    with mock.patch("app.services.command_service.CommandService", _CS), \
         mock.patch("app.services.nip05_access.ai_allowed", ai_allowed), \
         mock.patch("app.services.search_service.get_search_service", lambda db: _Search()):
        resp = asyncio.run(R.chat_assist(R.AssistReq(**body), db=None, user=user))
    code, data = (200, resp) if isinstance(resp, dict) else (resp.status_code, json.loads(resp.body))
    return code, data, fetched


def _msgs(n, me_every=2, who="Alice"):
    return [{"me": i % me_every == 0, "text": f"message {i}", "who": who} for i in range(n)]


def test_reply_offers_three_drafts_for_the_medium_in_one_call():
    chat = _Chat(["1. On my way\n2. Give me ten minutes!\n3. Want me to bring anything?"])
    code, data, _ = _run({"action": "reply", "medium": "telegram",
                          "messages": [{"me": False, "text": "Are you coming?"}], "count": 3}, chat)
    assert code == 200 and data["choices"] == ["On my way", "Give me ten minutes!", "Want me to bring anything?"]
    assert data["content"] == "On my way" and len(chat.calls) == 1
    system = chat.calls[0][0]["content"]
    assert "Telegram chat message replies" in system and "never invent" in system
    code, _, _ = _run({"action": "reply", "medium": "dm", "messages": [{"me": False, "text": "hi"}]}, _Chat(["1. hey"]))
    assert code == 200


def test_the_texts_prompt_is_unchanged():
    msgs = texts.build_messages([(False, "hi")])
    assert msgs[0]["content"].startswith('You draft text-message (SMS) replies for the user')
    assert texts.build_choice_messages([(False, "hi")], 3)[0]["content"].startswith("You draft text-message (SMS) replies")


def test_summarize_reads_a_bounded_tail_with_speakers():
    chat = _Chat(["- Alice asked about Friday\n- Waiting on you: confirm the time"])
    items = _msgs(200)
    items[-1] = {"me": False, "text": "Bob here: is Friday still on?", "who": "Bob"}
    code, data, _ = _run({"action": "summarize", "medium": "telegram", "messages": items}, chat)
    assert code == 200 and data["summary"].startswith("- Alice asked")
    prompt = chat.calls[0][1]["content"]
    assert "message 199" not in prompt and "Bob: Bob here: is Friday still on?" in prompt
    assert "message 120" in prompt and "message 119" not in prompt, "the newest 80 are kept, the oldest dropped"
    assert "Me: message 198" in prompt
    code, data, _ = _run({"action": "summarize", "messages": []}, _Chat([]))
    assert code == 400 and "nothing in this chat" in data["error"]


def test_links_are_read_newest_first_through_the_guarded_fetcher():
    yt, art, old, lan = ("https://youtu.be/dQw4w9WgXcQ", "https://example.com/news/story.",
                         "https://old.example/a", "http://192.168.0.1/admin")
    items = [{"me": False, "text": f"see {old}"}, {"me": True, "text": f"also {lan}"},
             {"me": False, "text": f"watch {yt} and read {art}"}]
    pages = {yt: {"url": yt, "title": "A video", "content": "transcript " * 30},
             "https://example.com/news/story": {"url": art, "title": "Story", "content": "story text " * 30}}
    chat = _Chat(["The video says hello.", "The story says things."])
    code, data, fetched = _run({"action": "links", "messages": items}, chat, pages=pages)
    assert code == 200
    assert fetched == [yt, "https://example.com/news/story", lan], "newest first, three at most, punctuation trimmed"
    got = {l["url"]: l for l in data["links"]}
    assert got[yt]["summary"] == "The video says hello." and got[yt]["title"] == "A video"
    assert "blocked" in got[lan]["error"] and "summary" not in got[lan], "an unreadable link is not summarized"
    assert len(chat.calls) == 2
    code, data, fetched = _run({"action": "links", "messages": [{"me": False, "text": "no links here"}]}, _Chat([]))
    assert code == 400 and "no links" in data["error"] and fetched == []


def test_the_gate_refuses_before_the_model_and_probe_asks_nothing():
    chat = _Chat(["1. x"])
    code, data, _ = _run({"action": "reply", "messages": [{"me": False, "text": "hi"}]}, chat, allowed=False)
    assert code == 403 and chat.calls == []
    code, data, _ = _run({"probe": True}, chat, allowed=True)
    assert data == {"ok": True, "allowed": True} and chat.calls == []
    code, data, _ = _run({"action": "reply", "messages": [{"me": False, "text": "hi"}]}, chat, user=None)
    assert code == 401 and chat.calls == []
    code, _, _ = _run({"action": "delete-everything", "messages": [{"me": False, "text": "hi"}]}, chat)
    assert code == 400


def test_nothing_anybody_wrote_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    items = [{"me": False, "text": SECRET + " https://example.com/x"}]
    pages = {"https://example.com/x": {"url": "https://example.com/x", "title": "t", "content": SECRET * 10}}
    for action in ("reply", "summarize", "links"):
        _run({"action": action, "messages": items}, _Chat(["1. " + SECRET, SECRET]), pages=pages)
    assert SECRET not in caplog.text and "4471" not in caplog.text


# ---- ✨ on a desktop window: the answer comes back to the panel --------------------------------------

def test_window_answers_from_the_selection_first_and_names_every_window():
    chat = _Chat(["- The build failed on a missing header"])
    wins = [{"title": "Terminal", "view": "terminal", "kind": "PosterChan app",
             "selection": "fatal error: foo.h: No such file", "text": "IGNORED visible text"},
            {"title": "Firefox", "kind": "native app", "text": ""}]
    code, data, _ = _run({"action": "window", "windows": wins, "instruction": "Explain output"}, chat)
    assert code == 200 and data["answer"] == "- The build failed on a missing header"
    assert len(chat.calls) == 1
    system, user = chat.calls[0][0]["content"], chat.calls[0][1]["content"]
    assert "ONLY the window content" in system and "never claim it was done" in system
    assert "fatal error: foo.h" in user and "IGNORED" not in user, "a selection wins over the whole window"
    assert '"Firefox" (native app)' in user and "only the window's name is known" in user
    assert user.rstrip().endswith("Request: Explain output")


def test_window_context_is_bounded():
    wins = [{"title": f"W{i}", "text": "x" * 9000} for i in range(6)]
    ctx = svc.window_context(wins)
    assert len(ctx) == svc.WINDOW_MAX
    assert all(len(c[3]) <= svc.WINDOW_EACH for c in ctx)
    assert sum(len(c[3]) for c in ctx) <= svc.WINDOW_TOTAL


def test_window_needs_a_question_and_the_gate_still_applies():
    code, data, _ = _run({"action": "window", "windows": [{"title": "Notes", "text": "hi"}], "instruction": "  "}, _Chat([]))
    assert code == 400 and "Ask something" in data["error"]
    chat = _Chat(["should not be called"])
    code, _, _ = _run({"action": "window", "windows": [{"title": "Notes"}], "instruction": "Summarize"}, chat, allowed=False)
    assert code == 403 and chat.calls == []


# ---- "Add to Calendar": the model proposes, the server validates -------------------------------------

@pytest.mark.parametrize("reply,want", [
    ('{"title":"Dentist","date":"2026-10-02","start":"14:30","end":"15:00","allDay":false,"location":"Main St","notes":"bring card"}',
     {"title": "Dentist", "date": "2026-10-02", "start": "14:30", "end": "15:00", "allDay": False, "location": "Main St", "notes": "bring card"}),
    ('Sure! Here it is: {"title":"Rent due","date":"2026-10-01","start":"","end":""} hope that helps',
     {"title": "Rent due", "date": "2026-10-01", "start": "", "end": "", "allDay": True, "location": "", "notes": ""}),
    ('{"title":"Call","date":"2026-10-02","start":"25:00","end":"26:00"}',          # an impossible time is dropped
     {"title": "Call", "date": "2026-10-02", "start": "", "end": "", "allDay": True, "location": "", "notes": ""}),
    ('{"title":"Call","date":"2026-10-02","start":"15:00","end":"14:00"}',          # an end before the start is dropped
     {"title": "Call", "date": "2026-10-02", "start": "15:00", "end": "", "allDay": False, "location": "", "notes": ""}),
    ('{"none": true}', None),
    ('{"title":"Lunch","date":"next friday"}', None),                                # not a date
    ('{"title":"","date":"2026-10-02"}', None),                                      # no title
    ('I could not find anything.', None),
])
def test_an_extracted_event_is_validated_field_by_field(reply, want):
    assert svc.parse_event(reply) == want


def test_window_event_passes_today_and_the_answer_and_says_when_there_is_none():
    chat = _Chat(['{"title":"Dentist","date":"2026-10-01","start":"09:00","end":"09:30"}'])
    code, data, _ = _run({"action": "window_event", "today": "2026-09-30",
                          "windows": [{"title": "Mail", "text": "See you tomorrow at 9 for your cleaning"}],
                          "answer": "- Dentist tomorrow at 9"}, chat)
    assert code == 200 and data["event"]["date"] == "2026-10-01" and data["event"]["start"] == "09:00"
    user = chat.calls[0][1]["content"]
    assert user.startswith("Today is 2026-09-30.") and "cleaning" in user and "Dentist tomorrow" in user
    assert "never invent a time or a place" in chat.calls[0][0]["content"]
    code, data, _ = _run({"action": "window_event", "windows": [{"title": "Mail", "text": "hello"}]}, _Chat(['{"none":true}']))
    assert code == 422 and "No event" in data["error"]


# ---- ✨ INTERACTIVE: tasks and steps the panel turns into buttons --------------------------------------
# "we need interactive Agentic features with buttons, not loading up AI Chat" / "Extract Tasks need to be
# functional and actually useful". The model only PROPOSES; everything is validated before the panel
# sees it, so a malformed step is no step rather than a wrong command in somebody's terminal.

STEPS_REPLY = json.dumps({
    "answer": "- Two invoices are due\n- One reply is owed",
    "tasks": [{"text": "Pay the Comcast invoice", "due": "2026-10-03", "who": "Dana"},
              {"text": "Reply to Sam about payroll", "due": "next friday", "who": ""},
              {"text": "", "due": "2026-10-04"}, "not a dict"],
    "steps": [{"do": "command", "label": "Show disk use", "text": "df -h"},
              {"do": "command", "label": "Two lines", "text": "rm -rf /tmp/x\nreboot"},
              {"do": "insert", "label": "Insert reply", "text": "Thanks Sam, on it."},
              {"do": "open", "label": "Open Calendar", "text": "Calendar"},
              {"do": "open", "label": "Open evil", "text": "javascript:alert(1)"},
              {"do": "launch-missiles", "label": "x", "text": "y"},
              {"do": "search", "label": "Look it up", "text": "comcast invoice due date"}]})


def test_window_steps_validates_every_task_and_step():
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Mail", "text": "invoices"}],
                          "instruction": "Extract tasks", "today": "2026-10-01"}, _Chat([STEPS_REPLY]))
    assert code == 200 and data["ok"] is True
    assert data["answer"].startswith("- Two invoices are due")
    assert data["tasks"] == [{"text": "Pay the Comcast invoice", "due": "2026-10-03", "who": "Dana"},
                             {"text": "Reply to Sam about payroll", "due": "", "who": ""}], \
        "an unparseable date is dropped, an empty task and a non-dict are discarded"
    kinds = [(s["do"], s["text"]) for s in data["steps"]]
    assert ("command", "df -h") not in kinds, "commands are only proposed when the panel allows them"
    assert ("insert", "Thanks Sam, on it.") in kinds and ("open", "calendar") in kinds
    assert all(k != "launch-missiles" for k, _ in kinds) and ("open", "javascript:alert(1)") not in kinds
    assert len(data["steps"]) <= svc.STEP_MAX


def test_commands_only_for_a_terminal_and_only_one_safe_line():
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Terminal", "view": "terminal", "text": "$ "}],
                          "instruction": "Plan a fix", "commands": True}, _Chat([STEPS_REPLY]))
    cmds = [s["text"] for s in data["steps"] if s["do"] == "command"]
    assert cmds == ["df -h"], "a multi-line command is never offered as a button"


def test_the_prompt_offers_commands_only_when_allowed_and_carries_the_panels_memory():
    chat = _Chat([STEPS_REPLY])
    _run({"action": "window_steps", "windows": [{"title": "Terminal", "text": "out"}], "instruction": "Continue",
          "commands": True, "today": "2026-10-01",
          "history": [{"q": "Plan a fix", "a": "Check the disk", "did": ["ran `df -h`"]}]}, chat)
    system, user = chat.calls[0][0]["content"], chat.calls[0][1]["content"]
    assert '"command"' in system and "never claim anything was done" in system
    assert "Today is 2026-10-01." in user and "Earlier request: Plan a fix" in user and "ran `df -h`" in user
    assert "The window AS IT IS NOW" in user
    chat2 = _Chat([STEPS_REPLY])
    _run({"action": "window_steps", "windows": [{"title": "Mail", "text": "x"}], "instruction": "Summarize"}, chat2)
    assert '"command"' not in chat2.calls[0][0]["content"]


def test_a_reply_that_is_not_json_is_still_an_answer_and_its_bullets_become_tasks():
    plain = "Here is what needs doing:\n- Call the bank\n- Send the W-2 forms\n2) Book the venue"
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Mail", "text": "x"}],
                          "instruction": "Extract tasks"}, _Chat([plain]))
    assert data["answer"].startswith("Here is what needs doing")
    assert [t["text"] for t in data["tasks"]] == ["Call the bank", "Send the W-2 forms", "Book the venue"]
    assert data["steps"] == []
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Mail", "text": "x"}],
                          "instruction": "Summarize"}, _Chat([plain]))
    assert data["tasks"] == [], "bullets become tasks only when tasks were asked for"


CONTROLS = [{"ref": 1, "role": "textbox", "label": "Title", "value": ""},
            {"ref": 2, "role": "list", "label": "Colour", "value": "Red"},
            {"ref": 3, "role": "checkbox", "label": "Pin it", "value": "off"},
            {"ref": 4, "role": "button", "label": "Save", "value": ""},
            {"ref": "x", "role": "button", "label": "Broken"}, "not a dict"]
ACT_REPLY = json.dumps({"answer": "Filling it in.", "steps": [
    {"do": "fill", "ref": 1, "text": "Groceries"},
    {"do": "choose", "ref": 2, "text": "Blue"},
    {"do": "toggle", "ref": 3, "on": True},
    {"do": "click", "ref": 4, "label": "Save it"},
    {"do": "click", "ref": 99, "label": "A button the model made up"},
    {"do": "fill", "ref": 1, "text": ""},
    {"do": "click", "ref": "4; drop table"}]})


def test_the_window_can_be_operated_through_the_controls_it_sent():
    """'need way to interact with the current window and do stuff': the prompt lists the window's
    controls by number, and a step may name only a number that was sent."""
    chat = _Chat([ACT_REPLY])
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Notes", "text": "x"}],
                          "instruction": "make a groceries note", "controls": CONTROLS}, chat)
    assert code == 200
    system, user = chat.calls[0][0]["content"], chat.calls[0][1]["content"]
    assert '"click"' in system and '"fill"' in system and "Use ONLY the numbered controls" in system
    assert '[1] textbox "Title"' in user and '[2] list "Colour" = "Red"' in user and "Broken" not in user
    got = [(s["do"], s["ref"], s["target"], s["text"], s["on"]) for s in data["steps"]]
    assert got == [("fill", 1, "Title", "Groceries", False), ("choose", 2, "Colour", "Blue", False),
                   ("toggle", 3, "Pin it", "", True), ("click", 4, "Save", "", False)], got
    assert data["steps"][3]["label"] == "Save it"


def test_without_controls_no_action_step_is_offered_or_accepted():
    chat = _Chat([ACT_REPLY])
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Notes", "text": "x"}],
                          "instruction": "do it"}, chat)
    assert '"click"' not in chat.calls[0][0]["content"]
    assert data["steps"] == [], "a click on a control nobody listed was accepted"


def test_the_ai_can_scroll_and_press_keys_and_sees_each_controls_section():
    """'when done, improve agent features for posterchan windows': a list that is longer than the window
    is reached by scrolling, a search box is submitted with Enter, and two controls with the same label
    are told apart by the section they sit in."""
    controls = [{"ref": 1, "role": "textbox", "label": "Search", "value": "", "near": "Find people"},
                {"ref": 2, "role": "button", "label": "Save", "near": "Profile"},
                {"ref": 3, "role": "button", "label": "Save", "near": "Relays"}]
    reply = json.dumps({"answer": "ok", "steps": [
        {"do": "fill", "ref": 1, "text": "alice"},
        {"do": "press", "ref": 1, "text": "enter"},
        {"do": "press", "ref": 1, "text": "Delete"},
        {"do": "scroll", "text": "down"},
        {"do": "scroll", "text": "sideways"},
        {"do": "press", "ref": 9, "text": "Enter"}]})
    chat = _Chat([reply])
    code, data, _ = _run({"action": "window_steps", "windows": [{"title": "Social", "text": "x"}],
                          "instruction": "find alice", "controls": controls}, chat)
    system, user = chat.calls[0][0]["content"], chat.calls[0][1]["content"]
    assert '"press"' in system and '"scroll"' in system
    assert '[2] button "Save" (in "Profile")' in user and '[3] button "Save" (in "Relays")' in user
    got = [(s["do"], s["ref"], s["text"]) for s in data["steps"]]
    assert got == [("fill", 1, "alice"), ("press", 1, "Enter"), ("scroll", 0, "down")], got


# "Do it for me is completely useless": the node's own model, asked to act on a Notes window, replied
# with two right steps and NO final "}". The strict parse threw them away and the panel showed raw JSON.
_REAL_REPLY = ('{"answer": "I\'ll help you navigate this Notes window. The most useful next step is to search '
               'for existing bug notes or start a new one.\\n- Search \'Bugs\' in the All notes section", '
               '"tasks": [{"text": "Search for \'Bugs\' in All notes", "due": "", "who": ""}], '
               '"steps": [{"do": "click", "label": "Search your notes...", "text": "Bugs", "ref": 2, "on": false}, '
               '{"do": "click", "label": "New note", "ref": 1, "on": true}]')
_NOTES_CONTROLS = [{"ref": 1, "role": "button", "label": "New note"},
                   {"ref": 2, "role": "textbox", "label": "Search your notes..."},
                   {"ref": 3, "role": "button", "label": "Import"}]


def test_a_reply_missing_its_last_brace_still_gives_its_steps():
    from app.services.chat_assist_service import parse_steps
    res = parse_steps(_REAL_REPLY, controls=_NOTES_CONTROLS)
    assert [(s["do"], s["ref"], s["text"]) for s in res["steps"]] == [("fill", 2, "Bugs"), ("click", 1, "")], res
    assert res["answer"].startswith("I'll help you") and "{" not in res["answer"], res["answer"]


def test_near_miss_replies_are_read_the_way_the_model_meant_them():
    from app.services.chat_assist_service import parse_steps
    fenced = '```json\n{"answer":"ok","steps":[{"do":"type","ref":2,"text":"bugs"},{"do":"press","ref":1,"text":"New note"},]}\n```'
    res = parse_steps(fenced, controls=_NOTES_CONTROLS)
    assert [(s["do"], s["ref"]) for s in res["steps"]] == [("fill", 2), ("click", 1)], res
    # A real key press stays a key press.
    res = parse_steps('{"answer":"","steps":[{"do":"press","ref":2,"text":"Enter"}]}', controls=_NOTES_CONTROLS)
    assert res["steps"][0]["do"] == "press" and res["steps"][0]["text"] == "Enter", res
    # A made-up control is still refused.
    assert parse_steps('{"answer":"x","steps":[{"do":"click","ref":99}]}', controls=_NOTES_CONTROLS)["steps"] == []


def test_an_unreadable_json_reply_is_never_shown_as_the_answer():
    from app.services.chat_assist_service import parse_steps
    res = parse_steps('{"answer": "half", "steps": [{"do": "click", "ref": ', controls=_NOTES_CONTROLS)
    assert "{" not in res["answer"] and res["steps"] in ([], res["steps"]), res


def test_a_placeholder_is_never_typed_into_a_field():
    """Measured from the node's model: 'fill' with "[Your note title here]" -- typing that helps nobody."""
    from app.services.chat_assist_service import parse_steps
    res = parse_steps('{"answer":"ok","steps":[{"do":"click","ref":1},{"do":"fill","ref":2,"text":"[Your note title here]"},'
                      '{"do":"fill","ref":2,"text":"<search term>"},{"do":"fill","ref":2,"text":"groceries"}]}',
                      controls=_NOTES_CONTROLS)
    assert [(s["do"], s["text"]) for s in res["steps"]] == [("click", ""), ("fill", "groceries")], res


def test_the_prompt_tells_the_model_to_put_actions_in_steps():
    from app.services.chat_assist_service import build_steps_messages, window_context
    system = build_steps_messages(window_context([{"title": "Notes", "text": "x"}]), "do it", controls=_NOTES_CONTROLS)[0]["content"]
    assert "goes in \"steps\"" in system and "placeholder" in system


def test_each_post_s_reply_reaches_the_model_with_the_post_it_belongs_to():
    """Every post has its own "reply"; the client describes each by the start of its post (up to 90
    characters, so "who" AND "what" fit). Cut at 40 the two read the same, which is a guess again."""
    from app.services.chat_assist_service import build_steps_messages, window_context
    ctl = [{"ref": 3, "role": "button", "label": "reply",
            "near": "carol @carol 20s Meeting with the relay operators moved to Friday 3pm."},
           {"ref": 9, "role": "button", "label": "reply",
            "near": "bob @bob 50s Anyone know a good self-hosted calendar that syncs with my phone?"}]
    user = build_steps_messages(window_context([{"title": "Social", "text": "x"}]), "reply to carol", controls=ctl)[1]["content"]
    assert '[3] button "reply" (in "carol @carol 20s Meeting with the relay operators moved to Friday 3pm.")' in user
    assert "self-hosted calendar" in user


# ---- Repairs measured against the node's own model (scripts/eval_window_ai.py); each fails without its rule.
_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "window_ai")


def _fixture(name):
    with open(os.path.join(_FIX, name + ".json")) as fh:
        return json.load(fh)["controls"]


def _steps(raw_steps, controls, instruction):
    from app.services.chat_assist_service import parse_steps
    return parse_steps(json.dumps({"answer": "ok", "tasks": [], "steps": raw_steps}), controls=controls,
                       instruction=instruction)["steps"]


def _ref(controls, label, near=""):
    return next(c["ref"] for c in controls if c["label"].lower() == label.lower() and near in c.get("near", ""))


def test_reply_then_type_elsewhere_becomes_one_fill_on_that_reply():
    """Measured: {click carol's Reply, fill the NEW POST box} -- pressed as proposed, a post, not a reply."""
    c = _fixture("global")
    reply, box = _ref(c, "reply", "relay operators"), _ref(c, "How was your weekend?")
    got = _steps([{"do": "click", "ref": reply, "label": "Open reply box"},
                  {"do": "fill", "ref": box, "text": "I'll bring the numbers", "label": "Type reply"}],
                 c, "reply to the post about the relay operators")
    assert [(s["do"], s["ref"], s["text"]) for s in got] == [("fill", reply, "I'll bring the numbers")]


def test_destruction_nobody_asked_for_is_dropped_and_asked_for_is_kept():
    c = _fixture("notes")
    wipe = _ref(c, "Delete all notes & files")
    step = [{"do": "click", "ref": wipe, "label": "Clear everything"}]
    assert _steps(step, c, "clean this up a bit") == []
    assert [s["ref"] for s in _steps(step, c, "delete all my notes")] == [wipe]


def test_the_quoted_words_are_what_gets_typed():
    c = _fixture("global")
    box = _ref(c, "How was your weekend?")
    got = _steps([{"do": "fill", "ref": box, "text": "Good morning! How's everyone?", "label": "Write"}],
                 c, "post 'good morning nostr'")
    assert got[0]["text"] == "good morning nostr"
    # An apostrophe in a word is not a quote.
    got = _steps([{"do": "fill", "ref": box, "text": "see you there", "label": "Write"}], c, "say I'll be there")
    assert got[0]["text"] == "see you there"


def test_the_tab_the_request_names_wins():
    c = _fixture("global")
    got = _steps([{"do": "click", "ref": _ref(c, "Nostrverse"), "label": "Go to Nostrverse"}], c, "show me what's trending")
    assert got[0]["ref"] == _ref(c, "Trending")


def test_a_click_labelled_exactly_as_another_control_goes_to_that_control():
    """Measured: {"do":"click","ref":<New post>,"label":"Post"} -- New post opens an empty box and sends nothing."""
    c = _fixture("global")
    got = _steps([{"do": "click", "ref": _ref(c, "New post"), "label": "Post"}], c, "post it")
    assert got[0]["ref"] == _ref(c, "Post")


# ---- Mail and Settings (fixtures captured 2026-10-04); each step below is a reply the node's model gave.
def _item_ref(controls, word):
    return next(c["ref"] for c in controls if c["role"] == "item" and word in c["label"].lower())


def test_open_moves_a_ticked_select_box_to_its_row():
    """'open the receipt email' -> the model ticked a row's Select box (0/3)."""
    c = _fixture("mail")
    box = _ref(c, "Select", "receipt")
    got = _steps([{"do": "toggle", "ref": box, "on": True, "label": "Select receipt"}], c, "open the receipt email")
    assert [(s["do"], s["ref"]) for s in got] == [("click", _item_ref(c, "receipt"))], got


def test_select_turns_opening_a_row_into_ticking_it():
    """'select the lunch email' -> the model opened the row (0/3)."""
    c = _fixture("mail")
    got = _steps([{"do": "click", "ref": _item_ref(c, "lunch"), "label": "Open Dana's email"}], c, "select the lunch email")
    assert [(s["do"], s["ref"], s["on"]) for s in got] == [("toggle", _ref(c, "Select", "Lunch"), True)], got


def test_the_row_they_named_wins():
    """'open the receipt email' -> the model opened the INVOICE row beside one that says receipt."""
    c = _fixture("mail")
    got = _steps([{"do": "click", "ref": _item_ref(c, "invoice"), "label": "Open sender's email"}], c, "open the receipt email")
    assert [s["ref"] for s in got] == [_item_ref(c, "receipt")], got


def test_search_means_the_search_box():
    """'search my email for invoices' -> the model opened the invoice email (0/3)."""
    c = _fixture("mail")
    got = _steps([{"do": "click", "ref": _item_ref(c, "invoice"), "label": "Open invoice"}], c, "search my email for invoices")
    box = _ref(c, "Search all email accounts")
    assert [(s["do"], s["ref"], s["text"]) for s in got] == [("fill", box, "invoices"), ("press", box, "Enter")], got


def test_show_me_a_tab_clicks_it():
    """'show my relay settings' -> nothing (0/3), or a click on the neighbouring 'poster.place'."""
    c = _fixture("settings")
    relays = _ref(c, "Relays")
    assert [(s["do"], s["ref"]) for s in _steps([], c, "show my relay settings")] == [("click", relays)]
    wrong = _ref(c, "poster.place")
    assert [s["ref"] for s in _steps([{"do": "click", "ref": wrong, "label": "Current relay"}], c, "show my relay settings")] == [relays]


def test_looking_types_nothing():
    """'show my relay settings' -> a fill of the Instance box with a relay URL the model made up."""
    c = _fixture("settings")
    got = _steps([{"do": "fill", "ref": _ref(c, "Instance"), "text": "https://nostr-relay.example", "label": "Set new relay"}],
                 c, "show my relay settings")
    assert not [s for s in got if s["do"] == "fill"], got
    # ...while a request that DOES write keeps its fill.
    got = _steps([{"do": "fill", "ref": _ref(c, "Instance"), "text": "poster.place", "label": "Set instance"}],
                 c, "set my instance to 'poster.place'")
    assert [s["do"] for s in got] == ["fill"], got


def test_enter_on_a_button_is_a_click():
    """'post ...' -> {"do":"press","ref":<Post>,"text":"Enter"} pressed a key on a button."""
    c = _fixture("global")
    post = _ref(c, "Post")
    got = _steps([{"do": "fill", "ref": _ref(c, "How was your weekend?"), "text": "good morning nostr"},
                  {"do": "press", "ref": post, "text": "Enter", "label": "Post"}], c, "post 'good morning nostr'")
    assert [(s["do"], s["ref"]) for s in got][-1] == ("click", post), got


def test_a_broken_step_does_not_cost_the_tasks():
    """'Extract decisions and next actions' (2/4): both tasks were right; one step was malformed JSON and
    the whole reply -- tasks included -- was thrown away. This is the model's reply, cut where it was."""
    from app.services.chat_assist_service import parse_steps
    raw = ('{"answer": "Extracting decisions and next actions from the Social window.", "tasks": [{"text": '
           '"Find a self-hosted calendar that syncs with phone", "due": "", "who": ""}, {"text": "Move relay '
           'operator meeting to Friday 3pm", "due": "2026-10-08", "who": ""}], "steps": [{"do": "open", "label": '
           '"Open post about calendar"}, "ref": 9, "on": true}, {"do": "click", "label": "View replies", "ref": 18, "on')
    res = parse_steps(raw, want_tasks=True, controls=_fixture("global"), instruction="Extract decisions and next actions from this window.")
    assert [t["text"] for t in res["tasks"]] == ["Find a self-hosted calendar that syncs with phone",
                                                  "Move relay operator meeting to Friday 3pm"], res
