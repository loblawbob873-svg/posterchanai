"""✨ do-it steps: the model's MEASURED near-misses (scripts/eval_window_ai.py, 2026-10-06) come out right.

Each case is a real reply from this node's model for a real captured window (tests/fixtures/window_ai/),
run through the shipped parse_steps. A person meets these as a button that does the wrong thing or stops
one press short of finishing.
"""
import json
from pathlib import Path

from app.services.chat_assist_service import parse_steps

FIX = Path(__file__).parent / "fixtures" / "window_ai"


def _run(fixture, instruction, reply):
    f = json.loads((FIX / f"{fixture}.json").read_text())
    res = parse_steps(json.dumps(reply), False, False, f["controls"], instruction)
    lab = {c["ref"]: c["label"] for c in f["controls"]}
    return [(s["do"], lab.get(s.get("ref"), ""), s.get("text", "")) for s in res["steps"]]


def test_enter_after_typing_a_search_runs_that_search_and_opens_no_new_contact():
    got = _run("contacts", "find bob", {"answer": "", "steps": [
        {"do": "fill", "ref": 1, "text": "bob"}, {"do": "press", "ref": 2, "text": "Enter"}]})
    assert got == [("fill", "Search contacts", "bob"), ("press", "Search contacts", "Enter")], got


def test_asked_to_add_the_plan_ends_by_pressing_add():
    got = _run("budget", "add my paycheck of 2000 as income", {"answer": "", "steps": [
        {"do": "toggle", "ref": 8, "on": True}, {"do": "fill", "ref": 6, "text": "Paycheck"},
        {"do": "fill", "ref": 7, "text": "2000.00"}]})
    assert got[-1] == ("click", "Add", ""), got
    assert ("fill", "Amount", "2000.00") in got


def test_open_naming_a_control_is_a_click_on_it():
    got = _run("torrents", "show my downloads", {"answer": "", "steps": [{"do": "open", "ref": 1, "label": "Downloads tab"}]})
    assert got == [("click", "⬇ Downloads", "")], got


def test_a_tab_drawn_with_an_emoji_is_still_the_one_named():
    got = _run("torrents", "show my downloads", {"answer": "", "steps": [{"do": "click", "ref": 6}]})
    assert got == [("click", "⬇ Downloads", "")], got


def test_typing_on_a_keypad_presses_its_keys_and_equals():
    got = _run("calculator", "calculate 12 times 7", {"answer": "12 × 7 = 84", "steps": [
        {"do": "fill", "ref": 5, "text": "12"}, {"do": "press", "ref": 11, "text": "×"}, {"do": "fill", "ref": 1, "text": "7"}]})
    assert [g[1] for g in got] == ["1", "2", "×", "7", "="], got
    assert all(g[0] == "click" for g in got)


def test_no_keypad_no_expansion():
    got = _run("contacts", "find 12", {"answer": "", "steps": [{"do": "fill", "ref": 1, "text": "12"}]})
    assert got[0] == ("fill", "Search contacts", "12"), got


def test_a_destructive_verb_is_never_pressed_for_you():
    got = _run("notes-editor", "delete this note", {"answer": "", "steps": [{"do": "fill", "ref": 10, "text": "x"}]})
    assert not any(g[1] == "Delete note" for g in got), got


def test_an_invented_magnet_on_a_button_just_presses_the_button():
    got = _run("torrents", "add a magnet link", {"answer": "", "steps": [{"do": "fill", "ref": 6, "text": "magnet:xt9:..."}]})
    assert got == [("click", "Add torrent", "")], got


def test_find_news_about_x_searches_in_news():
    got = _run("websearch", "find news about bitcoin", {"answer": "", "steps": [
        {"do": "fill", "ref": 1, "text": "bitcoin"}, {"do": "click", "ref": 2}]})
    assert ("fill", "Search the web", "bitcoin") in got and ("click", "News", "") in got, got


def test_toggle_on_a_button_presses_it():
    got = _run("mail-reader", "mark it as unread", {"answer": "", "steps": [{"do": "toggle", "ref": 22, "on": True}]})
    assert got == [("click", "Mark unread", "")], got


def test_an_invented_value_typed_onto_a_button_is_just_a_press():
    got = _run("torrents", "add a magnet link", {"answer": "", "steps": [
        {"do": "fill", "ref": 6, "text": "magnet:xt1-2026-10-06-torrent-name.torrent"}]})
    assert got == [("click", "Add torrent", "")], got


def test_a_fill_labelled_as_another_box_goes_to_that_box():
    got = _run("notes-editor", "call this note Shopping list", {"answer": "", "steps": [
        {"do": "fill", "ref": 15, "text": "Shopping list", "label": "Note title"}]})
    assert got == [("fill", "Note title", "Shopping list")], got


def test_pressing_reply_again_after_filling_it_is_dropped():
    got = _run("mail-reader", "reply saying thanks, I'll pay it today", {"answer": "", "steps": [
        {"do": "fill", "ref": 27, "text": "Thanks! I'll pay it today."}, {"do": "press", "ref": 27, "text": "Enter"}]})
    assert got == [("fill", "Reply", "Thanks! I'll pay it today.")], got
