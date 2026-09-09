"""Messages opened from a launcher tile must not spend its history read on a CONNECTING socket.

    "when I click on Messages from the android launcher, it does not load messages for me"

The route was never the problem — the shipped phoneshell.js does reach `switchView('messages')`
(see tests/client/test_every_launcher_tile_lands_on_its_screen.py, which runs it per tile). What
arrived empty was the conversation list.

`ensureDMs` is the last entry-querying view in the client that never waited for a socket that can
answer, and it is the one most exposed to the gap: a tile lands the instant `pc-app-ready` fires,
which app.js dispatches in the same turn it called `connectRelays()`. See the .mjs for the whole
argument; it RUNS the shipped function.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_history_read_waits_and_the_live_subscription_does_not():
    result = subprocess.run(
        ["node", str(ROOT / "tests/client/dm_history_waits_for_a_socket_runtime.mjs")],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
