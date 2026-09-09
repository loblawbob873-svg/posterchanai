"""SELECTIVE PUSH: a closed phone must obey the Notifications tab.

The in-app gate (`notificationAllowed` in app.js) only ever governed alerts the OPEN client raised
for itself. A real push comes from `nostr_push_service._poll()` while the app may not be running at
all, and it sent EVERY matching event to EVERY device with no per-type filter — so the phone buzzed
for every like, repost and zap whatever the toggles said. That is the bug these cover.

The two rules that matter are opposite in sign, and both are tested here:
  * an explicit `false` silences that type on THAT DEVICE, and
  * anything else — unset, unparseable, unknown — still sends, because a silenced alert cannot be
    told apart from a lost one and the failure would be a DM that never arrived.
"""
import json

import pytest

from app.services import push_prefs


# ---- classification: the toggle and the wording must not drift ----------------------------------

@pytest.mark.parametrize("ev,expected", [
    ({"kind": 7, "content": "❤️"}, "likes"),
    ({"kind": 6}, "reposts"),
    ({"kind": 9735}, "zaps"),
    ({"kind": 1111}, "replies"),
    ({"kind": 42}, "channels"),
    ({"kind": 4}, "dm"),
    ({"kind": 1059}, "dm"),
    ({"kind": 1, "tags": [["e", "abc"]]}, "replies"),      # NIP-10: an `e` tag makes it a reply
    ({"kind": 1, "tags": [["p", "abc"]]}, "mentions"),     # p-tagged with no thread = a mention
    ({"kind": 1, "tags": []}, "mentions"),
])
def test_every_pushed_kind_lands_on_the_toggle_that_describes_it(ev, expected):
    assert push_prefs.push_type(ev) == expected


def test_a_kind_with_no_toggle_is_never_silenced():
    """A voice call has no switch in the tab, and must ring whatever else is off. `push_type`
    answering "" is what carries that, so it is asserted rather than assumed."""
    assert push_prefs.push_type({"kind": 25050}) == ""
    assert push_prefs.allows('{"likes": false, "replies": false}', "") is True
    assert push_prefs.allows('{"likes": false}', "call") is True


def test_a_malformed_event_is_classified_rather_than_raising():
    assert push_prefs.push_type({}) == ""
    assert push_prefs.push_type({"kind": "not a number"}) == ""
    assert push_prefs.push_type({"kind": 1, "tags": "not a list"}) == "mentions"


# ---- the gate ----------------------------------------------------------------------------------

def test_an_explicit_false_is_the_only_thing_that_silences():
    assert push_prefs.allows(json.dumps({"likes": False}), "likes") is False
    assert push_prefs.allows(json.dumps({"likes": True}), "likes") is True
    assert push_prefs.allows(json.dumps({"replies": False}), "likes") is True


@pytest.mark.parametrize("stored", [None, "", "{}", "not json at all", "[]", '"a string"', "null"])
def test_it_fails_open_because_a_silenced_alert_looks_exactly_like_a_lost_one(stored):
    """Never configured, half-written, or corrupt — all of it still sends.

    This is deliberate and is the whole safety argument. Too many notifications is a complaint
    somebody can make; one that never arrived is a bug nobody can report, because from the outside
    "I turned that off" and "it was dropped" are the same silence.
    """
    assert push_prefs.allows(stored, "likes") is True


def test_clean_stores_known_booleans_and_drops_everything_else():
    got = push_prefs.clean({"likes": False, "replies": True, "sound": "chime",
                            "unknown_type": False, "zaps": "no"})
    assert got == {"likes": False, "replies": True}


def test_clean_survives_junk():
    for junk in (None, "", 5, [], "likes"):
        assert push_prefs.clean(junk) == {}


def test_the_vocabulary_matches_the_client_exactly():
    """One list of names. A toggle labelled "Likes and reactions" that the server spells
    differently silences nothing, and no test on either side alone would see it."""
    from pathlib import Path
    import re
    app = (Path(__file__).resolve().parents[1] / "static/js/client/app.js").read_text(encoding="utf-8")
    block = app[app.index("const _NOTIFICATION_TYPES"):app.index("const _NOTIFICATION_SOUNDS")]
    client = set(re.findall(r"\['([a-z]+)',", block))
    assert client == set(push_prefs.PUSH_TYPES), (
        "the client's toggles and the server's push types have drifted: "
        f"client-only={client - set(push_prefs.PUSH_TYPES)}, "
        f"server-only={set(push_prefs.PUSH_TYPES) - client}")
