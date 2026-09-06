"""ONE REFUSED PERMISSION AT LINE 56 KILLED SIGNING, UPLOADING AND POSTING.

Reported, over an evening, as: "why is the signer extension so shit, I can't fucking send any posts
from webui now", "firefox is like all shit with the signer extension now", "can't even upload an
attachment" -- with the console saying only

    signEvent failed (0 bytes): Could not establish connection. Receiving end does not exist.

`installRelayIdentity()` runs at TOP LEVEL, roughly 1,200 lines above
`runtime.onMessage.addListener`. On Firefox it registers a BLOCKING webRequest listener, and
Manifest V3 does not grant `webRequestBlocking` to an ordinary add-on, so the call raises

    Using the 'blocking' extraInfoSpec requires the 'webRequestBlocking' permission

The exception propagates out of the `const relayIdentityReady = …` initialiser and aborts the whole
script. The message listener is never reached, so EVERY message from every page answers "receiving
end does not exist" -- for ever, on every wake, with nothing in the page console naming the cause.
Signing, uploading (a Blossom auth event is signed) and posting all die together, and the symptom
points at the messaging layer instead of at a refused permission.

The header rewrite it was doing is a nicety: it labels this extension's relay sockets so a
PosterChan-only relay recognises them. Signing is local and needs no relay at all. A nicety must
never cost the whole extension.

THE SIMULATION LOADS THE REAL SCRIPTS, in manifest order, with the browser refusing exactly as
Firefox does, and counts the listeners. A source-text assertion cannot see this: every line of the
file was correct, and the file simply stopped running before reaching the important one.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "tests/extension_background_boot_sim.mjs"


def _boot(mode):
    out = subprocess.run(["node", str(SIM), "extension", mode],
                         cwd=ROOT, text=True, capture_output=True, timeout=180)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_refused_permission_does_not_stop_the_extension_answering():
    r = _boot("refuse")
    assert r["failure"] is None, f"background.js aborted: {r['failure']}"
    assert r["messageListeners"] == 1, (
        "no runtime.onMessage listener — every message will answer "
        "'Could not establish connection. Receiving end does not exist.'")


def test_it_says_so_rather_than_failing_silently():
    r = _boot("refuse")
    assert any("relay identity" in w for w in r["warned"]), r["warned"]


def test_every_background_script_still_loads():
    r = _boot("refuse")
    assert r["loaded"][-1] == "background.js", r["loaded"]


def test_nothing_changes_when_the_permission_is_granted():
    r = _boot("allow")
    assert r["failure"] is None and r["messageListeners"] == 1
    assert not r["warned"], f"warned on the working path: {r['warned']}"
