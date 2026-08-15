"""The desktop's "this machine just woke up" signal, end to end.

Run: venv-unified/bin/python -m pytest tests/test_desktop_wake.py

WHY IT EXISTS. Every reconnect path in the client hangs off `visibilitychange`, `online` or
`pageshow`. A desktop window that was never hidden fires none of them when the machine resumes, and
`online` only fires if Chromium decided the interface went down — which a suspend often does not do.
So the page had no way to learn its sockets had outlived a sleep. Reported as a NIP-46 signer that
stopped working overnight and came back only after a reload; the sockets were either closed with
nothing redialling them, or zombies (readyState 1, delivering nothing), and neither state announces
itself.

WHY IT IS A SOURCE TEST AND NOT A RUNNING ONE. Electron cannot run here — it needs a display — and
main.js does far too much at import to stub the way background.js is stubbed. What can still be
checked is the thing that actually breaks: this is a THREE-PIECE chain across three files (main
process → preload → page), joined by a channel name that appears in two of them and a function name
that appears in the other two. Rename either half and nothing errors anywhere: the machine wakes,
the page is told nothing, and the failure is indistinguishable from the bug this fixes.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


MAIN = _read("desktop", "main.js")
PRELOAD = _read("desktop", "preload.js")
APP = _read("static", "js", "client", "app.js")


def test_the_main_process_listens_for_the_os_resume():
    """powerMonitor is the ONLY source that knows. Nothing else in the app is told."""
    assert re.search(r"powerMonitor\.on\(\s*['\"]resume['\"]", MAIN), \
        "nothing subscribes to powerMonitor 'resume' — the machine wakes and no one is told"


def test_the_channel_name_is_the_same_on_both_sides():
    """A renamed channel is silent on both ends: send() succeeds, no listener ever fires."""
    sent = set(re.findall(r"webContents\.send\(\s*['\"](pc:wake)['\"]", MAIN))
    heard = set(re.findall(r"ipcRenderer\.on\(\s*['\"](pc:wake)['\"]", PRELOAD))
    assert sent, "main.js never sends pc:wake to the window"
    assert heard, "preload.js never listens for pc:wake"
    assert sent == heard, f"channel mismatch: main sends {sent}, preload hears {heard}"


def test_it_goes_to_every_window_not_just_the_first():
    """Popped-out windows (the stream, the chat) hold their own sockets and sleep with the rest."""
    block = MAIN[MAIN.index("function pushWake"):]
    block = block[:block.index("\n}")]
    assert "getAllWindows()" in block, "the wake goes to one window; the others stay asleep"
    assert "isDestroyed()" in block, "sending to a destroyed window throws and kills the loop"


def test_the_bridge_exposes_it_and_hides_the_event():
    """The page must never receive the IpcRendererEvent — same rule as onSyncNow/onStatus."""
    m = re.search(r"onWake:\s*\(fn\)\s*=>\s*\{(.+?)\n    \}", PRELOAD, re.S)
    assert m, "preload does not expose onWake on the pcShell bridge"
    body = m.group(1)
    assert "typeof fn !== 'function'" in body, "onWake does not check what it was handed"
    assert re.search(r"\(\)\s*=>\s*\{\s*try\s*\{\s*fn\(\)", body), \
        "onWake forwards the raw ipc event to the page"


def test_the_page_subscribes_and_reconnects():
    """The last link. Subscribing and then not reconnecting is the same as not subscribing."""
    m = re.search(r"pcShell\.onWake\s*\)\s*\n?\s*window\.pcShell\.onWake\(\s*\(\)\s*=>\s*\{(.+?)\}\)",
                  APP, re.S)
    assert m, "app.js never subscribes to pcShell.onWake"
    assert "_resumeRelay()" in m.group(1), \
        "the wake is received and nothing is redialled"


def test_the_signer_sockets_are_in_the_resume_path_at_all():
    """The bug underneath the bug: _resumeRelay woke the relay POOL and nothing else.

    A NIP-46 session's socket is not in that pool — neither the half that asks a signer nor the half
    that IS one — so a resumed machine had a healthy feed and a signer that could not be reached,
    which reads as the phone's fault and is not.
    """
    body = APP[APP.index("function _resumeRelay()"):]
    body = body[:body.index("\n  }")]
    assert "Relay.wake()" in body
    assert "Nip46.revive()" in body, "the remote-signer socket is not woken with the rest"
    assert "Nip46Signer.revive()" in body, "the socket this device SIGNS on is not woken with the rest"
