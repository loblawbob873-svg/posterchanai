"""THE BACKGROUND WORKER IS ASLEEP, NOT ABSENT — AND ONE ATTEMPT NEVER WAKES IT.

Reported from a real console, and it looked like five unrelated features breaking at once:

    could not establish your app session:
    Could not establish connection. Receiving end does not exist.
      __pcNostrProvider ... ensureAiSession ... loadThemeFromServer

`__pcNostrProvider` is OUR extension (extension/inject.js). MV3 evicts the background service worker
when it is idle, and the FIRST message after that eviction fails with exactly that text — while
being the very thing that starts the worker. `content.js` sent once and gave up, so the first signer
call after the browser had been sitting still simply failed.

Everything authenticated goes through that one call, so it took the whole app session with it: theme
sync ("theme sync skipped"), mail, the Monero wallet. It was reported as "Monero wallet is not even
loading" and cost a long time looking at monerod, the pool daemon and the wallet RPC — all healthy.

"Extension context invalidated" is the OTHER error and must NOT be retried: the content script has
been orphaned by an update, nothing it sends will ever arrive, and only reloading the page fixes it.
Retrying that one just delays an honest message.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTENT = (ROOT / "extension/content.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node unavailable")


def _run(fail_times: int, error: str) -> dict:
    """Drive the SHIPPED askWorker against a worker that fails `fail_times` times first."""
    at = CONTENT.index("  const ASLEEP =")
    end = CONTENT.index("  window.addEventListener('message', async (e) => {", at)
    src = CONTENT[at:end]
    script = f"""
      let calls = 0;
      const B = {{ runtime: {{ sendMessage: async () => {{
        calls++;
        if (calls <= {fail_times}) throw new Error({json.dumps(error)});
        return {{ ok: true, result: 'signed' }};
      }} }} }};
      {src}
      askWorker({{type:'nostr'}}).then(
        r => console.log(JSON.stringify({{ok:true, result:r, calls}})),
        e => console.log(JSON.stringify({{ok:false, error:String(e.message), calls}})));
    """
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_sleeping_worker_is_woken_by_the_retry():
    """THE BUG. One failure then success is the normal MV3 shape, and it must be invisible."""
    got = _run(1, "Could not establish connection. Receiving end does not exist.")
    assert got["ok"] is True, f"a sleeping worker still fails the call: {got}"
    assert got["calls"] == 2, "it did not retry"


def test_a_worker_that_needs_two_nudges_still_works():
    got = _run(2, "Could not establish connection. Receiving end does not exist.")
    assert got["ok"] is True and got["calls"] == 3


def test_it_gives_up_eventually_rather_than_retrying_for_ever():
    got = _run(99, "Could not establish connection. Receiving end does not exist.")
    assert got["ok"] is False
    assert got["calls"] <= 4, f"it retried {got['calls']} times — a stuck worker must not hang the page"


def test_an_orphaned_content_script_is_not_retried():
    """After an extension update the script is orphaned; nothing it sends will ever arrive. Retrying
    only delays the one message that helps."""
    got = _run(99, "Extension context invalidated.")
    assert got["ok"] is False
    assert got["calls"] == 1, "an unrecoverable error was retried"
    assert "reload this page" in got["error"], (
        "an orphaned script does not tell the user the one thing that fixes it")


def test_an_unrelated_error_is_not_swallowed_by_retries():
    got = _run(99, "some other failure")
    assert got["ok"] is False and got["calls"] == 1


def test_the_signer_path_uses_it():
    """The wiring: a retry helper nothing calls fixes nothing. This is the call that carries every
    authenticated request in the app."""
    at = CONTENT.index("__pcnostr !== 'req'")
    window = CONTENT[at:at + 400]
    assert "askWorker({ type:'nostr'" in window, (
        "the signer call is back to a single sendMessage with no retry")
