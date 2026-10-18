"""The circuit breaker's pauses survive a restart.

server1, 2026-10-18: the breaker's pauses lived in memory only, so every restart (each deploy) re-dialled every dead
upstream from scratch -- ~400 failed TLS handshakes in the relay's first two minutes, re-learning what it had known a
minute before.
"""
import importlib
import time

from app.services.nostr import relay


def _restart():
    """A new process: the module imported afresh, with nothing in memory."""
    return importlib.reload(relay)


def test_a_pause_and_its_escalation_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_RELAY_BREAKER_FILE", str(tmp_path / "breaker.json"))
    r = _restart()
    dead = "wss://dead.example/"
    monkeypatch.setattr(r, "_FAIL_DEBOUNCE", 0)
    for _ in range(r._RELAY_FAIL_THRESHOLD):
        r._note_relay_fail(dead)
    assert r._relay_paused(dead)
    streak = r._relay_pause_streak[dead]

    r = _restart()
    assert r._relay_paused(dead), "the restart forgot the pause and will dial a dead relay again"
    assert r._relay_pause_streak.get(dead) == streak, "the escalation started over"

    r._note_relay_ok(dead)                               # it came back: the next process must not hold it off
    r = _restart()
    assert not r._relay_paused(dead)


def test_an_expired_pause_is_not_brought_back(tmp_path, monkeypatch):
    path = tmp_path / "breaker.json"
    path.write_text('{"wss://old.example/": [%f, 3], "wss://now.example/": [%f, 1]}' % (time.time() - 5, time.time() + 600))
    monkeypatch.setenv("PC_RELAY_BREAKER_FILE", str(path))
    r = _restart()
    assert not r._relay_paused("wss://old.example/") and r._relay_paused("wss://now.example/")


def teardown_module(_m):
    import os
    os.environ.pop("PC_RELAY_BREAKER_FILE", None)
    importlib.reload(relay)                              # leave a clean module for the tests after these
