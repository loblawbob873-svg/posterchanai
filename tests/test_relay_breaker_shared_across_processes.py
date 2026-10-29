"""A relay that turns this node away is paused at once, and every process on the node hears about it.

server1, 2026-10-29: wss://asia.vectorapp.io answered every handshake with HTTP 403, and three processes (the app and
two bot processes) kept dialling it -- ~890 log lines in three hours. Two causes, each measured in the journal: a 403
went through the 3-strike ramp meant for outages (three more refused dials, through Tor and then direct, before each
pause), and each process ran its own breaker and wrote the shared file with ONLY its own state, so the escalation
started over at streak 1 whenever another process had just paused the relay.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEAD = "wss://refuses.example/nostr"


class _Status(Exception):
    def __init__(self, code):
        super().__init__("server rejected WebSocket connection: HTTP %d" % code)
        self.response = type("R", (), {"status_code": code})()


def _in_process(path, code):
    """Run `code` in a NEW python process with the breaker file at `path` -- a real second process, not a reload."""
    env = {**os.environ, "PC_RELAY_BREAKER_FILE": str(path), "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", "from app.services.nostr import relay as r\n" + code],
                       capture_output=True, text=True, timeout=60, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_a_403_pauses_the_relay_on_the_first_refusal():
    from app.services.nostr import relay as r
    dials = []

    async def connect(url, **kw):
        dials.append(kw.get("proxy"))
        raise _Status(403)

    orig, conn_kw = r.peer_ua.connect, r._conn_kw
    r.peer_ua.connect, r._conn_kw = connect, (lambda relay, direct: {"proxy": None})
    r._relay_paused_until.pop(DEAD, None); r._relay_pause_streak.pop(DEAD, None)
    try:
        with pytest.raises(_Status):
            asyncio.run(r._open(DEAD, False))
        assert r._relay_paused(DEAD), "a relay that refused us is still being dialled"
        assert len(dials) == 1
    finally:
        r.peer_ua.connect, r._conn_kw = orig, conn_kw
        r._note_relay_ok(DEAD)


def test_an_outage_still_takes_three_strikes():
    from app.services.nostr import relay as r

    async def connect(url, **kw):
        raise OSError("connection refused")

    orig, conn_kw = r.peer_ua.connect, r._conn_kw
    r.peer_ua.connect, r._conn_kw = connect, (lambda relay, direct: {"proxy": None})
    try:
        with pytest.raises(OSError):
            asyncio.run(r._open(DEAD, False))
        assert not r._relay_paused(DEAD), "one blip paused a relay"
    finally:
        r.peer_ua.connect, r._conn_kw = orig, conn_kw
        r._note_relay_ok(DEAD)


def test_a_pause_one_process_learns_holds_the_others_off(tmp_path):
    path = tmp_path / "breaker.json"
    # The other process is already running (imported before the pause), as the app and the bots are.
    waiter = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\nfrom app.services.nostr import relay as r\nr._BREAKER_SYNC_SEC = 0\n"
         "print('ready', flush=True)\nsys.stdin.readline()\nprint(r._relay_paused(%r), r._relay_pause_streak.get(%r))\n"
         % (DEAD, DEAD)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT,
        env={**os.environ, "PC_RELAY_BREAKER_FILE": str(path), "PYTHONPATH": str(ROOT)})
    try:
        assert waiter.stdout.readline().strip() == "ready"
        _in_process(path, "r._note_relay_refused(%r)" % DEAD)
        out, _ = waiter.communicate("go\n", timeout=60)
    finally:
        waiter.kill()
    assert out.split() == ["True", "1"], ("another process kept dialling a relay this node had paused", out)


def test_one_process_saving_never_erases_anothers_pauses(tmp_path):
    path = tmp_path / "breaker.json"
    _in_process(path, "r._note_relay_refused('wss://a.example/')")
    _in_process(path, "r._note_relay_refused('wss://b.example/')")
    state = json.loads(path.read_text())
    assert set(state) == {"wss://a.example/", "wss://b.example/"}, state


def test_the_escalation_continues_where_another_process_left_it(tmp_path):
    path = tmp_path / "breaker.json"
    path.write_text(json.dumps({DEAD: [time.time() + 1, 3]}))      # paused by another process, about to expire
    out = _in_process(path, "import time\ntime.sleep(1.2)\nr._note_relay_refused(%r)\nprint(r._relay_pause_streak[%r])"
                      % (DEAD, DEAD))
    assert out == "4", ("the pause started over at the bottom of the ladder", out)


def test_a_recovery_clears_only_that_relay(tmp_path):
    path = tmp_path / "breaker.json"
    _in_process(path, "r._note_relay_refused('wss://a.example/')\nr._note_relay_refused('wss://b.example/')")
    _in_process(path, "r._note_relay_ok('wss://a.example/')")
    assert set(json.loads(path.read_text())) == {"wss://b.example/"}
