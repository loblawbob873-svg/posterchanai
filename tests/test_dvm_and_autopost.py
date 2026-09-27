"""The DVM answers every job it takes, and autopost posts one in-character note.

Run: venv-unified/bin/python -m pytest tests/test_dvm_and_autopost.py

DVM (NIP-90): runs the shipped process_job_requests() over the game harness's relay. A burst of more
jobs than the per-poll cap used to LOSE the ones past it -- the capped request was marked seen and the
cursor jumped to "now" -- so the burst test is the regression.
"""
import sys
import types

import pytest

from tests.botgame_harness import BOT_PK, GameWorld


@pytest.fixture
def dvm(monkeypatch, tmp_path):
    w = GameWorld(monkeypatch, tmp_path, "dvmListener", "process_job_requests")
    asked = []
    monkeypatch.setattr(w.game, "generate_reply", lambda prompt, **k: asked.append(prompt) or f"ANSWER {len(asked)}")
    monkeypatch.setattr(w.game, "is_ai_configured", lambda: True)
    monkeypatch.setattr(w.game, "_SEEN", set())
    monkeypatch.setattr(w.game, "_since", None)
    return w, asked


def _job(w, who, kind=5050, text="write a haiku"):
    return w.publish(who, kind, "", tags=[("i", text, "text"), ("p", BOT_PK)])


def test_a_text_job_gets_a_result_and_feedback(dvm):
    w, asked = dvm
    alice = w.player("alice")
    req = _job(w, alice)
    w.tick()
    res = w.events(6050)
    assert len(res) == 1 and res[0]["content"] == "ANSWER 1"
    tags = res[0]["tags"]
    assert ["e", req["id"]] in tags and ["p", alice.pk] in tags and ["i", "write a haiku", "text"] in tags
    status = [t[1] for e in w.events(7000) for t in e["tags"] if t[0] == "status"]
    assert "processing" in status and "success" in status


def test_a_summary_job_asks_for_a_summary(dvm):
    w, asked = dvm
    _job(w, w.player("alice"), kind=5001, text="a long article")
    w.tick()
    assert asked and asked[0].startswith("Summarize") and "a long article" in asked[0]


def test_its_own_and_unsupported_requests_are_ignored(dvm):
    w, asked = dvm
    from tests.botgame_harness import BOT_SK, Player
    me = Player("bot", 0x42)
    assert me.pk == BOT_PK
    _job(w, me)
    w.publish(w.player("alice"), 5999, "x", tags=[("i", "x", "text")])
    w.tick()
    assert asked == [] and w.events(6050) == []


def test_a_burst_over_the_cap_is_answered_in_full(dvm):
    w, asked = dvm
    cap = w.game._MAX_PER_POLL
    who = [w.player(f"p{i}") for i in range(cap + 2)]
    reqs = [_job(w, p, text=f"job {i}") for i, p in enumerate(who)]
    w.tick()
    assert len(w.events(6050)) == cap
    w.tick()
    answered = {t[1] for e in w.events(6050) for t in e["tags"] if t[0] == "e"}
    assert answered == {r["id"] for r in reqs}, f"{len(reqs) - len(answered)} job(s) past the cap were lost"


def test_a_job_with_no_input_is_told_so(dvm):
    w, asked = dvm
    w.publish(w.player("alice"), 5050, "", tags=[("p", BOT_PK)])
    w.tick()
    assert asked == [] and any(["status", "error"] in e["tags"] for e in w.events(7000))


# ---- autopost ---------------------------------------------------------------------------------

@pytest.fixture
def autopost(monkeypatch):
    import os
    monkeypatch.syspath_prepend(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "botframework"))
    monkeypatch.setenv("PROMPT", "You are Jonny Fever, a drunk know-it-all roofer.")
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    monkeypatch.delenv("PLEROMA_ENDPOINT", raising=False)
    monkeypatch.setenv("AUTO_POST_TOPICS", "roofing, dating\nDetroit")
    for m in ("config", "autopost"):
        sys.modules.pop(m, None)
    import autopost as ap
    posted, seeds = [], []
    fake = types.ModuleType("nostr")
    fake.post_image_to_fediverse = lambda text, *a, **k: posted.append(text)
    monkeypatch.setitem(sys.modules, "nostr", fake)
    monkeypatch.setattr(ap, "generate_reply", lambda seed, **k: seeds.append(seed) or "  Shingles don't lie.  ")
    return ap, posted, seeds


def test_autopost_posts_one_note_on_a_topic(autopost):
    ap, posted, seeds = autopost
    ap.autopost()
    assert posted == ["Shingles don't lie."]
    assert any(t in seeds[0] for t in ("roofing", "dating", "Detroit")), "no topic in the seed"


def test_autopost_topics_accept_lines_and_commas(autopost):
    ap, _, _ = autopost
    assert ap._topics() == ["roofing", "dating", "Detroit"]


def test_autopost_posts_nothing_when_generation_is_empty(autopost, monkeypatch):
    ap, posted, _ = autopost
    monkeypatch.setattr(ap, "generate_reply", lambda seed, **k: "   ")
    ap.autopost()
    assert posted == []


def test_autopost_preview_prints_and_never_posts(autopost, capsys):
    ap, posted, _ = autopost
    ap.autopost(print_only=True)
    out = capsys.readouterr().out
    assert ap.PREVIEW_BEGIN in out and "Shingles don't lie." in out and posted == []
