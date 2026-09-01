"""THE MESSAGE YOU JUST SENT MUST BE IN THE THREAD.

Reported as "i send dm to user, then the conversation goes blank".

The pane renders `dmPeers.get(pk)` and, when the same peer is already mounted, replaces the contents
of `#dm-msgs` in place. So if our own copy of the message never reaches `dmPeers`, that replacement
writes an EMPTY list — the composer stays, the chrome stays, and the conversation above it goes
blank. On a thread with no history there is nothing left on screen at all.

`ingestWrap` has several honest ways to decline, and every one of them returned false into a call
that ignored the answer:

  * the unwrap throws — a remote signer that timed out, a crypto worker busy verifying the feed;
  * the outer id is already in `_wrapTried`;
  * the rumor comes back as something other than kind 14/15.

The wrap remains the real record. This only guarantees the ECHO, keyed on the same outer id so the
copy that comes back from the relay de-duplicates against it rather than appearing twice.

The runtime file drives the shipped `sendDm` with an ingest that refuses.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
RUNTIME = ROOT / "tests/client/dm_echo_runtime.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_refused_ingest_still_shows_your_message():
    done = subprocess.run([NODE, str(RUNTIME)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok" in done.stdout


def test_the_answer_from_ingest_is_actually_used():
    """It was awaited and thrown away — the bug in one line."""
    send = APP_JS[APP_JS.index("async function sendDm(pk, text){"):]
    send = send[:send.index("dmInboxRelays(pk)")]
    assert "const echoed = await ingestWrap(toSelf, false)" in send, (
        "sendDm ignores whether its own copy was ingested again")
    assert "if(!echoed) _dmEcho(pk, text, toSelf && toSelf.id)" in send


def test_the_echo_carries_the_wrap_id_so_the_relay_copy_is_a_duplicate():
    """A different id would show the message twice the moment the relay echoes it back — which is
    the other half of this failure and easy to introduce while fixing the first."""
    fn = APP_JS[APP_JS.index("function _dmEcho(pk, text, id){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "arr.find(m => m.id === id)" in fn, "the echo can be added twice"
    assert "mine:true" in fn and "nip17:true" in fn


def test_the_echo_is_ordered_with_the_rest_of_the_thread():
    """Bubbles are grouped and day-separated by time; an entry appended out of order breaks the
    grouping and can render under yesterday's separator."""
    fn = APP_JS[APP_JS.index("function _dmEcho(pk, text, id){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "arr.sort(" in fn


def test_it_refuses_without_the_two_things_it_needs():
    """A missing peer or a wrap with no id would push an entry nothing can match or address."""
    fn = APP_JS[APP_JS.index("function _dmEcho(pk, text, id){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "if(!pk || !id) return false;" in fn
