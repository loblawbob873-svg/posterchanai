"""The timeline and Notes ✨ answers are checked against what was SENT, whatever the model says.

The model names posts and notes by number; these are the rules that keep an answer pointing at something
real (tests/client/test_window_ai_timeline_and_notes_full_app.py covers what the person sees):
  * a number that is not on screen is dropped -- a chip that opens nothing is worse than no chip;
  * a post addressed to the person is always in "What needs me", whatever the model picked;
  * a post that literally says what was searched for is always found;
  * replies that came back as broken JSON are still replies, never "the AI did not answer";
  * a tidied note does not repeat its own title, and a continued checklist stays a checklist.
"""
from app.services import chat_assist_service as svc

POSTS = svc.feed_posts([
    {"who": "Alice", "text": "Anyone know a good Wayland compositor for an old laptop?"},
    {"who": "Bob", "text": "did you ever get the Arc A770 running llama.cpp?", "to_me": True},
    {"who": "Carol", "text": "Bitcoin fees are low again"},
    {"who": "Me", "text": "my own post", "mine": True, "to_me": True},
])


def test_posts_are_numbered_and_my_own_post_is_never_addressed_to_me():
    assert [p["n"] for p in POSTS] == [1, 2, 3, 4]
    assert POSTS[3]["mine"] and not POSTS[3]["to_me"]


def test_a_number_that_is_not_on_screen_is_dropped():
    out = '{"topics":[{"title":"Linux","summary":"x","posts":[1,9,"2",0]},{"title":"Ghost","posts":[42]}]}'
    assert svc.parse_feed("digest", out, POSTS) == {"topics": [{"title": "Linux", "summary": "x", "posts": [1, 2]}]}


def test_a_post_addressed_to_me_is_always_in_what_needs_me():
    got = svc.parse_feed("needs", '{"items":[{"n":1,"why":"asks for a compositor"},{"n":4,"why":"mine"}]}', POSTS)
    assert [i["n"] for i in got["items"]] == [1, 2], got        # 2 is to me (the floor); 4 is my own post
    assert svc.parse_feed("needs", "nothing", POSTS)["items"] == [{"n": 2, "why": "addressed to you"}]


def test_a_post_that_says_the_words_is_always_found():
    assert svc.parse_feed("find", '{"posts":[]}', POSTS, "wayland compositor") == {"posts": [1]}
    assert svc.parse_feed("find", '{"posts":[3]}', POSTS, "crypto") == {"posts": [3]}


def test_replies_survive_broken_json_and_prose():
    broken = '{"replies": ["Try labwc, it is light.", "Which X220 GPU?",]}'
    assert svc.parse_feed("reply", broken, POSTS)["replies"] == ["Try labwc, it is light.", "Which X220 GPU?"]
    assert svc.parse_feed("reply", "Sure — labwc is great on old hardware.", POSTS)["replies"] == [
        "Sure — labwc is great on old hardware."]


def test_the_reply_prompt_holds_only_the_post_being_answered():
    msgs = svc.build_feed_messages(POSTS, "reply", "", 1)
    assert "Wayland" in msgs[1]["content"] and "Bitcoin" not in msgs[1]["content"]


def test_notes_are_found_by_number_too():
    notes = svc.feed_posts([{"who": "Home wifi", "text": "password: hunter2"}, {"who": "Groceries", "text": "milk"}], 80)
    msgs = svc.build_feed_messages(notes, "find", "wifi", 0, "notes")
    assert "[1] Home wifi: password: hunter2" in msgs[1]["content"]
    assert svc.parse_feed("find", '{"posts":[]}', notes, "wifi") == {"posts": [1]}


def test_a_tidied_note_does_not_repeat_its_title():
    assert svc.parse_note("tidy", "# Trip ideas\n- Lisbon", "Trip ideas", "x") == {"body": "- Lisbon"}
    assert svc.parse_note("tidy", "Lisbon\n- Porto", "Trip ideas", "x") == {"body": "Lisbon\n- Porto"}
    assert svc.parse_note("checklist", "- [ ] Check flights\n\nTitle: Trip ideas", "Trip ideas", "x") == {"body": "- [ ] Check flights"}


def test_a_continued_checklist_stays_a_checklist():
    got = svc.parse_note("continue", "[ ] Rome\n[ ] Barcelona\n[ ] [ ]\n- [ ] Book", "T", "- [ ] Lisbon")
    assert got == {"append": "- [ ] Rome\n- [ ] Barcelona\n- [ ] Book"}


def test_a_title_is_one_clean_line():
    assert svc.parse_note("title", '"Weekend trip ideas."\nmore', "", "x") == {"title": "Weekend trip ideas"}


def test_write_takes_the_heading_as_the_title():
    assert svc.parse_note("write", "# Beach weekend\n- towel\n- sunscreen", "", "") == {
        "title": "Beach weekend", "body": "- towel\n- sunscreen"}


def test_a_digest_survives_the_broken_json_the_model_actually_writes():
    # Measured 3 times in 4: an unquoted summary, and no comma between two topics.
    out = ('{\n  "topics": [\n    {\n      "title": "Linux & Open Source Dev",\n      "summary": Users discussing '
           'Wayland compositors and a meetup.\n      "posts": [1, 2]\n    }\n    {\n      "title": "Crypto",\n'
           '      "summary": "Fees are low",\n      "posts": [3, 9]\n    }\n  ]\n}')
    assert svc.parse_feed("digest", out, POSTS) == {"topics": [
        {"title": "Linux & Open Source Dev", "summary": "Users discussing Wayland compositors and a meetup.", "posts": [1, 2]},
        {"title": "Crypto", "summary": "Fees are low", "posts": [3]}]}
    assert svc.parse_feed("find", '{"posts": [1, 3,]', POSTS, "zzz") == {"posts": [1, 3]}


def test_a_calendar_occurrence_knows_when_it_ends():
    """ical.js read DTSTART only, so every occurrence lasted no time: "When am I free?" showed the hour of a
    14:00-15:00 meeting as free ("08:00–14:00, 14:00–20:00", measured)."""
    import json
    import subprocess
    from pathlib import Path
    js = Path(__file__).resolve().parents[1] / "static/js/client/ical.js"
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:e1\r\nDTSTART:20261009T140000\r\nDTEND:20261009T150000\r\n"
           "RRULE:FREQ=WEEKLY;COUNT=2\r\nSUMMARY:standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    out = subprocess.run(["node", "-e", "const I=require(%s);const o=I.occurrences(I.parseResource({ics:%s}),"
                          "new Date(2026,9,1),new Date(2026,10,1));console.log(JSON.stringify(o.map(x=>(x.end-x.start)/60000)))"
                          % (json.dumps(str(js)), json.dumps(ics))], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [60, 60]


def test_a_calculation_survives_an_unquoted_field():
    # Measured 3 in 3: ```json {"expression": "(84.50*1.15)/3", "what": tip amount per person}```
    out = '```json\n{\n  "expression": "(84.50*1.15)/3",\n  "what": tip amount per person\n}\n```'
    assert svc.parse_calc(out) == {"expression": "(84.50*1.15)/3", "what": "tip amount per person"}
    assert svc.parse_contact('{"given": Bob, "family": "Smith"}', "Bob Smith")["given"] == "Bob"


def test_search_results_are_ranked_against_the_search_by_number():
    res = svc.feed_posts([{"who": "wiki.gentoo.org", "text": "Wayfire configuration — how to install"},
                          {"who": "example.org", "text": "Cookie recipes"}])
    msgs = svc.build_feed_messages(res, "needs", "gentoo wayfire config", 0, "results")
    assert "[1] wiki.gentoo.org: Wayfire configuration" in msgs[1]["content"]
    assert "The search: gentoo wayfire config" in msgs[1]["content"]
    got = svc.parse_feed("needs", '{"items":[{"n":1,"why":"the setup guide"},{"n":7,"why":"ghost"}]}', res)
    assert got == {"items": [{"n": 1, "why": "the setup guide"}]}


def test_results_only_take_the_ranking_recipe():
    import asyncio
    import pytest as _pt
    with _pt.raises(svc.AssistError):
        asyncio.run(svc.window_feed(None, None, [{"who": "a", "text": "b"}], "reply", "", 1, "results"))
