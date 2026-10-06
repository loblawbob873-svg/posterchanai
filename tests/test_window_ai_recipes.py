"""The ✨ panel's buttons are RECIPES: one fixed outcome each, the model writes only the text.

"we need the actions to actually be useful". Measured against the node's own model (2026-10-06), the old
buttons were canned prompts sent through free-form step planning, and some results were UNSAFE, not just
weak: Email's "Draft reply" filled the account field with invented payment details ("Wire $45 to account
ending in 8921"), "Extract tasks" proposed steps that SEND a reply, Notes' "Do it for me" searched for its
own instruction. These pin the contract that makes that impossible:

  * the buttons around a result are built on the SERVER from what the client says is on screen -- a draft
    is one fill into the reply box (or through the window's own Reply), a summary can be saved to Notes,
    a tidied note replaces the note; nothing a recipe returns can press, send, delete or navigate;
  * a draft is told never to invent amounts, accounts, dates or promises, and may leave [placeholders];
    a summary is not (asked to, the model printed "[placeholder]" as content);
  * to-dos come from their own short prompt and never carry window steps, whatever the model writes;
  * the text is cleaned of "Here is a draft:" preambles and quotes, and a checklist is always "- [ ]".
"""
import asyncio

import pytest

from app.services import chat_assist_service as svc

DM = [{"title": "Messages", "view": "messages", "kind": "PosterChan app", "selection": "",
       "text": "Dana: are you coming Saturday? can you bring the projector?"}]

UNSAFE = {"click", "press", "toggle", "choose", "open", "search", "command"}


def _run(monkeypatch, reply, **kw):
    async def fake_chat(db, user, msgs, temp):
        fake_chat.msgs = msgs
        return reply
    monkeypatch.setattr(svc, "_chat", fake_chat)
    res = asyncio.run(svc.window_recipe(None, None, DM, **kw))
    return res, fake_chat.msgs


def test_a_draft_goes_into_the_open_reply_box_in_one_step(monkeypatch):
    res, _ = _run(monkeypatch, 'Here is a draft reply:\n"Yes! I\'ll bring the projector."',
                  recipe="draft", box_ref=7, box_label="Message Dana")
    assert res["answer"] == "Yes! I'll bring the projector."
    assert res["steps"] == [{"do": "fill", "ref": 7, "target": "Message Dana", "label": "Put it in the reply box",
                             "text": "Yes! I'll bring the projector.", "on": False}]


def test_a_draft_with_the_box_closed_goes_through_the_windows_own_reply(monkeypatch):
    res, _ = _run(monkeypatch, "Sure, see you then.", recipe="draft", reply_ref=12, box_label="Reply")
    assert [(s["do"], s["ref"]) for s in res["steps"]] == [("fill", 12)]


def test_a_draft_with_nowhere_to_go_is_offered_to_copy(monkeypatch):
    res, _ = _run(monkeypatch, "Sure.", recipe="draft")
    assert res["steps"] == [{"do": "insert", "label": "Put it in the reply box", "text": "Sure."}]


def test_the_draft_prompt_forbids_inventing_and_allows_gaps_the_summary_does_not(monkeypatch):
    _, draft = _run(monkeypatch, "ok", recipe="draft")
    _, summary = _run(monkeypatch, "- ok", recipe="summary")
    d, s = draft[0]["content"], summary[0]["content"]
    for prompt in (d, s):
        assert "Never invent" in prompt and "account or card numbers" in prompt
    assert "[placeholder]" in d and "[placeholder]" not in s


def test_a_summary_can_be_saved_and_does_nothing_else(monkeypatch):
    res, _ = _run(monkeypatch, "- Dana asks about Saturday\n- She wants the projector", recipe="summary")
    assert res["steps"] == [{"do": "note", "label": "Save summary to Notes", "text": res["answer"]}]


def test_tasks_never_carry_window_steps_even_when_the_model_writes_some(monkeypatch):
    reply = ('{"answer":"done","tasks":[{"text":"Bring the projector","due":"2026-10-10","who":""}],'
             '"steps":[{"do":"click","ref":3,"label":"Send"}]}')
    res, msgs = _run(monkeypatch, reply, recipe="tasks", today="2026-10-06")
    assert res["steps"] == [] and res["tasks"][0]["text"] == "Bring the projector"
    assert "Controls in window" not in msgs[1]["content"], "tasks rode the step-planning prompt"


def test_a_tidy_replaces_the_note_with_undo_and_a_checklist_is_always_ticks(monkeypatch):
    res, msgs = _run(monkeypatch, "Groceries\n- milk\n* eggs", recipe="checklist", text="milk eggs", box_ref=4)
    assert res["answer"] == "Groceries\n- [ ] milk\n- [ ] eggs"
    assert res["steps"][0]["do"] == "fill" and res["steps"][0]["ref"] == 4
    assert "milk eggs" in msgs[1]["content"]
    with pytest.raises(svc.AssistError):
        _run(monkeypatch, "x", recipe="tidy", text="")


def test_no_recipe_can_press_send_or_navigate(monkeypatch):
    for recipe, kw in (("summary", {}), ("draft", {"reply_ref": 3}), ("draft", {"box_ref": 2}),
                       ("tidy", {"text": "a b", "box_ref": 2}), ("checklist", {"text": "a b", "box_ref": 2})):
        res, _ = _run(monkeypatch, "- text", recipe=recipe, **kw)
        assert not [s for s in res["steps"] if s["do"] in UNSAFE], (recipe, res["steps"])


def test_the_route_knows_the_action():
    assert "window_recipe" in svc.ACTIONS
    from app.routers.chat_assist import AssistReq
    r = AssistReq(action="window_recipe", recipe="draft", reply_ref=3, box_ref=None, text="x")
    assert r.recipe == "draft" and r.reply_ref == 3


def test_explain_answers_the_buttons_fixed_question_and_can_be_saved(monkeypatch):
    res, msgs = _run(monkeypatch, "- Dentist Tuesday 3pm\n- Rent due on the 1st", recipe="explain",
                     note="What is coming up next in this calendar?")
    assert msgs[1]["content"].endswith("Question: What is coming up next in this calendar?")
    assert res["steps"] == [{"do": "note", "label": "Save to Notes", "text": res["answer"]}]
    with pytest.raises(svc.AssistError):
        _run(monkeypatch, "x", recipe="explain", note="")
