"""A Hold'em table left waiting on the BOT must wake itself up.

Run: venv-unified/bin/python -m pytest tests/test_holdem_resume.py

`_run_bot_turns` is only ever reached from an event handler: a move arrives, the bot answers. So if
the process stops while it is the BOT's own turn, nothing calls it again — the doc still says
`betting`, `to_act` is still the bot, and the table sits there for ever.

From the outside that is a game "in play" with nothing happening, and the player's move refused with
"⏳ Not your turn yet" — which is CORRECT, and reads as a bug, because it really is the bot's turn and
the bot is asleep. Reported three times across one day, and the cause was mundane: every deploy
restarts these bots, so eight deploys is eight chances to land mid-hand.

The listener is a bot-framework module with relay/signing globals, so it is imported with those
stubbed and `_resume_stalled_tables` is driven directly over synthetic docs. What is asserted is the
DECISION — which tables it touches — because touching the wrong one means the bot acts out of turn.
"""
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BF = ROOT / "botframework"


@pytest.fixture
def listener(monkeypatch):
    """Import holdemListener with its relay/crypto globals stubbed."""
    monkeypatch.syspath_prepend(str(BF))
    for mod in [m for m in list(sys.modules) if m.startswith("holdem")]:
        sys.modules.pop(mod, None)

    # The bot's own identity + a relay that returns whatever a test puts in `DOCS`.
    docs = []
    fake_nk = types.SimpleNamespace(
        _PUBKEY="b0" * 32, _SECKEY=b"\x01" * 32, _RELAYS=["ws://x"],
        _run=lambda coro: coro, _svc=types.SimpleNamespace(
            relay=types.SimpleNamespace(query=lambda relays, filters: list(docs), publish=lambda *a: None)),
        get_own_account=lambda: {"pubkey": "b0" * 32},
    )
    try:
        import holdemListener as H
    except Exception as e:                                     # pragma: no cover
        pytest.skip(f"holdemListener will not import here: {e}")
    monkeypatch.setattr(H, "_nk", fake_nk, raising=False)
    H._docs = docs
    return H


def _doc(dtag, state):
    return {"id": "e" + dtag, "pubkey": "b0" * 32, "kind": 30078,
            "tags": [["d", dtag]], "content": json.dumps(state)}


def _table(to_act, bot="b0" * 32, status="betting", seats=None):
    return {"status": status, "to_act": to_act, "bot": bot,
            "seats": seats if seats is not None else [bot, "aa" * 32]}


def test_a_table_waiting_on_the_bot_is_resumed(listener, monkeypatch):
    H = listener
    H._docs.append(_doc("pcai:holdem:g1", _table(to_act="b0" * 32)))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == ["g1"], "the stalled table was not woken"


def test_a_table_waiting_on_a_HUMAN_is_left_alone(listener, monkeypatch):
    """Acting here would have the bot play out of turn — worse than the stall."""
    H = listener
    H._docs.append(_doc("pcai:holdem:g2", _table(to_act="aa" * 32)))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == []


def test_a_finished_hand_is_left_alone(listener, monkeypatch):
    H = listener
    H._docs.append(_doc("pcai:holdem:g3", _table(to_act="b0" * 32, status="showdown")))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == []


def test_the_player_pointer_doc_is_not_mistaken_for_a_table(listener, monkeypatch):
    """`pcai:holdem:player:<pk>` shares the prefix and holds {"gameid": …}, not a table."""
    H = listener
    H._docs.append(_doc("pcai:holdem:player:" + "aa" * 32, {"gameid": "g9"}))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == []


def test_another_features_document_is_ignored(listener, monkeypatch):
    """The bot's key signs more than tables; a stray doc must not be read as one."""
    H = listener
    H._docs.append(_doc("pcai:blackjack:g4", _table(to_act="b0" * 32)))
    H._docs.append(_doc("pcai:note:abc", {"whatever": 1}))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == []


def test_a_bot_that_is_not_seated_is_not_resumed(listener, monkeypatch):
    """`to_act` naming a seat the bot no longer holds is a broken doc, not a turn."""
    H = listener
    H._docs.append(_doc("pcai:holdem:g5", _table(to_act="b0" * 32, seats=["aa" * 32])))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == []


def test_one_bad_document_does_not_stop_the_sweep(listener, monkeypatch):
    """A table further down the list still gets its turn."""
    H = listener
    H._docs.append(_doc("pcai:holdem:bad", {"junk": True}))
    H._docs.append({"id": "x", "pubkey": "b0" * 32, "kind": 30078,
                    "tags": [["d", "pcai:holdem:g6"]], "content": "not json"})
    H._docs.append(_doc("pcai:holdem:g7", _table(to_act="b0" * 32)))
    ran = []
    monkeypatch.setattr(H, "_run_bot_turns", lambda st, gid, pid: ran.append(gid))
    H._resume_stalled_tables()
    assert ran == ["g7"]


def test_a_failing_resume_does_not_take_the_rest_down(listener, monkeypatch):
    H = listener
    H._docs.append(_doc("pcai:holdem:g8", _table(to_act="b0" * 32)))
    H._docs.append(_doc("pcai:holdem:g9", _table(to_act="b0" * 32)))
    ran = []

    def boom(st, gid, pid):
        ran.append(gid)
        if gid == "g8":
            raise RuntimeError("the deck caught fire")

    monkeypatch.setattr(H, "_run_bot_turns", boom)
    H._resume_stalled_tables()
    assert ran == ["g8", "g9"]
