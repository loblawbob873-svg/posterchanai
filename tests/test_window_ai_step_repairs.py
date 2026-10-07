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


# ---- measured 2026-10-07 (scripts/eval_window_ai.py -n 3, baseline run) --------------------------------
def _run_full(fixture, instruction, raw_text, history=None):
    f = json.loads((FIX / f"{fixture}.json").read_text())
    return parse_steps(raw_text, False, "task" in instruction.lower() or "action" in instruction.lower(),
                       f["controls"], instruction, *([history] if history is not None else [])), {c["ref"]: c["label"] for c in f["controls"]}


def test_a_placeholder_typed_onto_a_button_is_just_a_press():
    # "add a magnet link" -> {"do":"fill","ref":6 "Add torrent","text":"[magnet link]"} was dropped whole
    # (a placeholder is never typed), so the button the request was about was never pressed (1 run in 3).
    got = _run("torrents", "add a magnet link", {"answer": "Adding magnet link.", "tasks": [], "steps": [
        {"do": "fill", "ref": 6, "text": "[magnet link]", "label": "Magnet"}]})
    assert got == [("click", "Add torrent", "")], got


def test_keys_typed_on_a_control_that_does_not_exist_are_still_keys():
    # "calculate 12 times 7": the 12 went to ref 0, which is no control -- dropped, so the keypad got × 7 =.
    got = _run("calculator", "calculate 12 times 7", {"answer": "12 × 7 = 84", "tasks": [], "steps": [
        {"do": "fill", "ref": 0, "text": "12", "label": "Enter 12"}, {"do": "press", "ref": 11, "text": "×", "label": "Multiply by"},
        {"do": "fill", "ref": 8, "text": "7", "label": "Enter 7"}]})
    assert [g[1] for g in got] == ["1", "2", "×", "7", "="], got


def test_looking_never_presses_enter_in_a_form():
    # "show my relay settings": the model typed an INVENTED relay URL into Instance and pressed Enter there.
    # The invented text was dropped (looking types nothing) but the Enter stayed -- submitting the settings
    # form -- and the Relays tab beside it was never opened.
    got = _run("settings", "show my relay settings", {
        "answer": "Your current relay is fixture.invalid. You can switch to another instance or use relays only.", "tasks": [],
        "steps": [{"do": "fill", "ref": 30, "text": "https://nostr-relay.flynn.xyz", "label": "Set new relay"},
                  {"do": "press", "ref": 34, "text": "Enter", "label": "Save settings"}]})
    assert got == [("click", "Relays", "")], got


def test_naming_a_note_titles_it_and_invents_no_body():
    # "call this note Shopping list" came back as a fill of the note's BODY with "Shopping list" and nine
    # grocery items nobody mentioned (2 runs in 3). A request that names the thing is a title.
    got = _run("notes-editor", "call this note Shopping list", {"answer": "Creating Shopping list note.", "tasks": [], "steps": [
        {"do": "fill", "ref": 15, "text": "Shopping list\n- Milk\n- Eggs\n- Bread\n- Coffee beans\n- Avocados\n- Chicken breast\n"
                                          "- Rice\n- Pasta\n- Vegetables (spinach, carrots, onions)", "label": "Shopping list"}]})
    assert got == [("fill", "Note title", "Shopping list")], got


def test_a_task_request_keeps_the_tasks_written_as_bullets_and_types_nothing():
    # "Extract decisions and next actions": the model answered with the two items as "- " bullets, left
    # "tasks" empty, and proposed typing "calendar recommendations" into a post's Reply.
    raw = ('{"answer": "Decisions and next actions extracted:\\n\\n- Decision: Meeting with relay operators moved to Friday '
           '3pm (2026-10-09)\\n- Next action: Find self-hosted calendar that syncs with phone (deadline unclear, likely soon)", '
           '"tasks": [], "steps": [{"do": "click", "ref": 18, "label": "Open reply box"}, {"do": "fill", "ref": 18, "text": '
           '"calendar recommendations", "label": "Ask for calendar recs"}]}')
    res, lab = _run_full("global", "Extract decisions and next actions from this window.", raw)
    assert any("relay operators" in t["text"] for t in res["tasks"]), res["tasks"]
    assert not any(s["do"] == "fill" for s in res["steps"]), res["steps"]


# ---- multi-round: the SECOND round of a task, with the panel's Continue instruction and the history os.js
# sends (scripts/eval_window_ai.py multi-* cases, measured 2026-10-07). The person's request reaches these
# repairs only through the history -- goal_of() -- so every one of these is also a test of that.
CONTINUE = "Continue: look at the window as it is now, check what the steps I took did, and propose what comes next."


def _round2(fixture, request, did, reply):
    hist = [{"q": request, "a": "", "did": did}]
    res, lab = _run_full(fixture, CONTINUE, json.dumps(reply), hist)
    return [(s["do"], lab.get(s.get("ref"), ""), s.get("text", "")) for s in res["steps"]]


def test_the_new_contact_form_is_filled_in_its_own_boxes_and_saved():
    # "Bob" was put on ref 4 (the phone's "label" field) with the step labelled "First name".
    got = _round2("contacts-new", "add Bob Smith 555-1234 to my contacts", ["pressed “New contact”"], {
        "answer": "Fill in the fields for Bob Smith and press Save.", "tasks": [], "steps": [
            {"do": "fill", "ref": 2, "text": "Smith", "label": "Last name"}, {"do": "fill", "ref": 3, "text": "555-1234", "label": "Phone number"},
            {"do": "fill", "ref": 4, "text": "Bob", "label": "First name"}, {"do": "press", "ref": 17, "text": "Enter", "label": "Save contact"}]})
    assert ("fill", "First", "Bob") in got and ("fill", "Last", "Smith") in got and ("fill", "Phone number", "555-1234") in got, got
    assert not any(g[1] == "label" for g in got), got
    assert got[-1] == ("click", "Save", ""), got


def test_a_filled_form_for_something_new_ends_on_its_save():
    # The form was filled and nothing pressed Save -- in a Continue round, whose instruction says nothing
    # about adding; the request that does is in the history.
    got = _round2("contacts-new", "add Bob Smith 555-1234 to my contacts", ["pressed “New contact”"], {
        "answer": "Fill in the fields and save.", "tasks": [], "steps": [
            {"do": "fill", "ref": 2, "text": "Smith", "label": "Last name"}, {"do": "fill", "ref": 1, "text": "Bob", "label": "First name"},
            {"do": "fill", "ref": 3, "text": "555-1234", "label": "Phone number"}]})
    assert got[-1] == ("click", "Save", ""), got


def test_the_dates_and_times_the_person_gave_go_in_the_event_and_nothing_is_invented():
    got = _round2("calendar-new-event", "add a dentist appointment on 2026-10-09 from 3pm to 4pm", ["pressed “New event”"], {
        "answer": "Fill in the appointment details and save.", "tasks": [], "steps": [
            {"do": "fill", "ref": 2, "text": "Dentist appointment", "label": "Title"}, {"do": "toggle", "ref": 4, "on": True, "label": "All day"},
            {"do": "fill", "ref": 5, "text": "10:30", "label": "From time"}, {"do": "fill", "ref": 6, "text": "11:30", "label": "To time"},
            {"do": "fill", "ref": 7, "text": "Downtown Dental Clinic", "label": "Location"}]})
    assert ("fill", "Day", "2026-10-09") in got and ("fill", "From", "15:00") in got and ("fill", "To", "16:00") in got, got
    assert not any(g[1] == "All day" for g in got), ("ticking All day hides the times asked for", got)
    assert not any(g[1] == "Where (optional)" for g in got), ("a clinic nobody named", got)
    assert got[-1] == ("click", "Save", ""), got


def test_a_note_called_something_gets_that_title():
    got = _round2("notes-editor", "write a new note called Groceries with milk and eggs", ["pressed “New note”"], {
        "answer": "The new note is open for editing. Type the groceries list here.", "tasks": [], "steps": [
            {"do": "fill", "ref": 15, "text": "milk, eggs", "label": "Groceries text"}]})
    assert ("fill", "Note title", "Groceries") in got and ("fill", "Note text", "milk, eggs") in got, got


def test_an_email_reaches_its_address_and_two_swapped_boxes_are_put_back():
    # Subject went on ref 6 (the body) labelled "Subject", the body on ref 5 (Subject); To was never filled.
    got = _round2("mail-compose", "email alice@x.test that the meeting moved to 3pm", ["pressed “Compose”"], {
        "answer": "Sending the email to alice@x.test", "tasks": [], "steps": [
            {"do": "fill", "ref": 6, "text": "Meeting moved to 3pm", "label": "Subject"},
            {"do": "fill", "ref": 5, "text": "The meeting has been rescheduled for 3pm.", "label": "Message body"}]})
    assert ("fill", "To (comma-separated)", "alice@x.test") in got, got
    assert ("fill", "Subject", "Meeting moved to 3pm") in got, got
    assert ("fill", "Write your message…", "The meeting has been rescheduled for 3pm.") in got, got
    assert not any(g[1] == "Send" for g in got), ("sending is the person's own press", got)


def test_a_continue_round_reads_the_request_from_the_history():
    from app.services.chat_assist_service import goal_of
    hist = [{"q": "add Bob Smith 555-1234 to my contacts", "a": "", "did": []}, {"q": CONTINUE, "a": "", "did": []}]
    assert goal_of(CONTINUE, hist) == "add Bob Smith 555-1234 to my contacts"
    assert goal_of("find bob", hist) == "find bob"
    assert goal_of(CONTINUE, []) == CONTINUE


def test_writing_to_a_new_address_starts_a_new_email_not_a_reply_to_a_row():
    # Round one of "email alice@x.test that the meeting moved to 3pm": the text "filled" onto Dana's row in
    # the inbox and her Select box ticked. (And the address must never be typed into the SEARCH box.)
    got = _run("mail", "email alice@x.test that the meeting moved to 3pm", {
        "answer": "Emailing Dana about the meeting time change.", "tasks": [], "steps": [
            {"do": "fill", "ref": 12, "text": "Meeting moved to 3pm. See you then.", "label": "New reply"},
            {"do": "toggle", "ref": 11, "on": True, "label": "Select Dana"}]})
    assert got == [("click", "Compose", "")], got


def test_a_reply_aimed_at_a_dropdown_after_pressing_reply_goes_on_the_reply():
    got = _run("mail-reader", "reply saying thanks, I'll pay it today", {"answer": "Replying to the invoice email.", "tasks": [], "steps": [
        {"do": "click", "ref": 27, "label": "Open invoice"},
        {"do": "fill", "ref": 1, "text": "Thanks for the invoice. I'll pay it today.", "label": "Reply text"}]})
    assert got == [("fill", "Reply", "Thanks for the invoice. I'll pay it today.")], got


def test_the_second_result_is_the_second_row():
    got = _run("websearch-results", "open the second result", {"answer": "Opening the Gentoo Wiki page on Wayfire configuration.",
               "tasks": [], "steps": [{"do": "click", "ref": 12, "label": "Open Gentoo Wiki"}]})
    assert len(got) == 1 and got[0][0] == "click" and got[0][1].startswith("wayfire.org wayfire.ini reference"), got


def test_a_row_click_when_the_request_names_the_rows_own_button_is_that_button():
    got = _run("websearch-results", "save the Gentoo Wiki result to my notes", {"answer": "Opening the Gentoo Wiki page on Wayfire configuration.",
               "tasks": [], "steps": [{"do": "click", "ref": 12, "label": "Open Gentoo Wiki"}]})
    assert got == [("click", "📓 Notes", "")], got
    f = json.loads((FIX / "websearch-results.json").read_text())
    assert next(c for c in f["controls"] if c["ref"] == 16)["near"].startswith("wiki.gentoo.org"), "the Notes of the Gentoo row"


def test_a_click_on_a_dropdown_naming_an_option_chooses_it():
    got = _run("translate", "translate between English and Spanish", {
        "answer": "Set up English-Spanish translation. Tap [3] to change second language to Spanish, then tap [5].", "tasks": [],
        "steps": [{"do": "click", "ref": 3, "label": "Select Spanish"}, {"do": "click", "ref": 5, "label": "Start listening"}]})
    assert got[0] == ("choose", "Second language", "Spanish"), got
