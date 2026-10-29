"""The desktop assistant's server half: the model only PROPOSES; only steps from the catalogue that name real things
survive (app ids the client listed, window numbers that exist, real zones), and what the person typed is never logged.
The model is replaced by canned replies shaped like a small local model's (fences, trailing junk, invented ids)."""
import asyncio
import json
import logging

import pytest

from app.services import chat_assist_service as svc
from app.services import desktop_assist as D

APPS = [{"view": "notes", "label": "Notes"}, {"view": "settings", "label": "Settings"}, {"view": "global", "label": "Social"}]
WINS = [{"title": "Social", "view": "global", "ws": 1, "focused": True}]


def _plan(steps, answer="ok"):
    return "```json\n" + json.dumps({"answer": answer, "steps": steps}) + "\n```"


def test_open_two_apps_and_snap_them_numbers_new_windows_after_the_open_ones():
    out = _plan([{"do": "open", "view": "notes"}, {"do": "open", "view": "settings"},
                 {"do": "snap", "win": 2, "zone": "left"}, {"do": "snap", "win": 3, "zone": "right"}])
    p = D.parse_steps(out, D.clean_apps(APPS), D.clean_windows(WINS), 1)
    assert [s["do"] for s in p["steps"]] == ["open", "open", "snap", "snap"]
    assert p["steps"][2] == {"do": "snap", "win": 2, "zone": "left", "label": "Snap to left half"}
    assert p["steps"][0]["label"] == "Open Notes"


@pytest.mark.parametrize("bad", [
    {"do": "open", "view": "bank"},                       # an app the client never listed
    {"do": "snap", "win": 7, "zone": "left"},             # no such window
    {"do": "snap", "win": 1, "zone": "upper-ish"},        # no such zone
    {"do": "workspace", "n": 40},
    {"do": "volume", "app": "notes", "level": 400},
    {"do": "volume", "app": "keylogger", "mute": True},
    {"do": "note", "text": "[your note here]"},           # a placeholder is a question, not a note
    {"do": "post", "text": ""},
    {"do": "rm -rf", "text": "/"},
    "open notes",
])
def test_anything_that_is_not_a_real_step_is_dropped(bad):
    p = D.parse_steps(_plan([bad]), D.clean_apps(APPS), D.clean_windows(WINS), 1)
    assert p["steps"] == [], p


def test_volume_mute_screenshot_and_a_new_workspace():
    p = D.parse_steps(_plan([{"do": "volume", "app": "music", "mute": True}, {"do": "volume", "app": "global", "level": 30.4},
                             {"do": "screenshot", "mode": "region"}, {"do": "workspace", "n": 2}]),
                      D.clean_apps(APPS), D.clean_windows(WINS), 1)
    assert p["steps"][0] == {"do": "volume", "app": "music", "mute": True, "label": "Mute music"}
    assert p["steps"][1]["level"] == 30 and p["steps"][3]["n"] == 2


def test_a_broken_reply_is_an_answer_with_no_steps_not_an_error():
    p = D.parse_steps("Sure! I opened Notes for you.", D.clean_apps(APPS), [], 1)
    assert p["steps"] == [] and p["answer"]


def test_at_most_six_steps():
    p = D.parse_steps(_plan([{"do": "record"}] * 20), D.clean_apps(APPS), [], 1)
    assert len(p["steps"]) == D.STEP_MAX


def test_the_prompt_lists_the_apps_and_windows_and_the_request():
    m = D.build_messages("put social on the left", D.clean_apps(APPS), D.clean_windows(WINS), 2, "2026-10-28")
    u = m[1]["content"]
    assert "notes: Notes" in u and "1. Social (app global, workspace 1, in front)" in u and "Workspaces: 2." in u
    assert u.endswith("Request: put social on the left")


def test_desktop_steps_calls_the_nodes_model_and_logs_only_sizes(monkeypatch, caplog):
    seen = {}

    async def fake_chat(db, user, msgs, temperature):
        seen["msgs"] = msgs
        return _plan([{"do": "open", "view": "notes"}], "Opening Notes.")
    monkeypatch.setattr(svc, "_chat", fake_chat)
    with caplog.at_level(logging.INFO):
        r = asyncio.run(D.desktop_steps(None, None, "open my secret diary notes", APPS, WINS, 1, "2026-10-28"))
    assert r["steps"] == [{"do": "open", "view": "notes", "label": "Open Notes"}] and r["answer"] == "Opening Notes."
    assert "secret diary" not in caplog.text, "the request was logged"
    assert "Request: open my secret diary notes" in seen["msgs"][1]["content"]


def test_an_empty_request_is_refused():
    with pytest.raises(svc.AssistError):
        asyncio.run(D.desktop_steps(None, None, "   ", APPS, [], 1))


def test_the_router_knows_the_action():
    assert "desktop_steps" in svc.ACTIONS
    src = open("app/routers/chat_assist.py").read()
    assert "desktop_assist.desktop_steps" in src


# ---- what this node's real model actually writes (scripts/audit_desktop_assistant.py, 2026-10-28: 3/15 before these
# repairs, 15/15 twice after). The intent was right; the fields were loose.
WINS2 = [{"title": "Social", "view": "global", "ws": 1, "focused": True}, {"title": "Terminal", "view": "terminal", "ws": 1}]
APPS2 = APPS + [{"view": "terminal", "label": "Terminal"}]


def _p(steps, instruction="", wins=WINS2):
    return D.parse_steps(_plan(steps), D.clean_apps(APPS2), D.clean_windows(wins), 1, instruction)["steps"]


def test_an_app_named_only_in_the_label_is_found():
    assert _p([{"do": "open", "label": "Notes"}]) == [{"do": "open", "view": "notes", "label": "Notes"}]


def test_a_window_named_by_its_title_is_found():
    assert _p([{"do": "close", "label": "Close Terminal"}])[0]["win"] == 2


def test_a_window_opened_by_an_earlier_step_is_found_by_name():
    st = _p([{"do": "open", "view": "notes"}, {"do": "snap", "label": "Notes left", "zone": "left"}])
    assert st[1]["win"] == 3


def test_mute_and_percent_spelled_as_words():
    assert _p([{"do": "volume", "app": "music", "level": "mute"}])[0]["mute"] is True
    assert _p([{"do": "volume", "app": "Social", "level": "20%"}])[0] == {"do": "volume", "app": "global", "level": 20,
                                                                          "label": "Volume Social"}


def test_part_of_the_screen_is_a_region_even_when_the_model_says_screen():
    assert _p([{"do": "screenshot", "mode": "screen"}], "take a screenshot of part of the screen")[0]["mode"] == "region"


def test_an_ambiguous_name_is_dropped_not_guessed():
    assert _p([{"do": "open", "label": "notes or settings"}]) == []
    assert _p([{"do": "close", "label": "close everything"}]) == []
