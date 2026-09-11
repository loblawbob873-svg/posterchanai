""""FOR SOME REASON, USE MY OWN RELAYS GOT ENABLED AGAIN!" — reported twice, two different causes.

The first was `_persistAuthRelays` reading the sign-in screen's relay box, which is PRE-FILLED with
the server's defaults, and counting an unrecorded seed as a deliberate choice
(tests/client/test_signing_in_is_not_choosing_your_relays.py). That was fixed. This is the other
one, and fixing the first is exactly why it looked fixed and came back.

`seedRelaysFromNip65` merges the user's published kind-10002 into the saved list. It also turned
the SWITCH on whenever the saved list was empty, reasoning that an empty list means "no
configuration to override". It does not:

  * an empty list is what you have IMMEDIATELY AFTER turning your own relays off —
    `_dropLegacyAutoRelays` writes literally `relays: []` and `relaysEnabled: false` — so the
    repair that switched it off was undone by this function on the very next pass, for ever;
  * and it caught everybody who had never configured relays here at all but whose OTHER client had
    published a NIP-65, which is most people.

Publishing a relay list says where to find you. Choosing to talk ONLY to your own relays is a
different statement, and only the person can make it.

THE RUNTIME BESIDE THIS FILE RUNS THE SHIPPED FUNCTION. Reading the source is what missed this the
first time — the fix went into a function with the same symptom and a different name, and nothing
executed the one that was actually flipping the switch.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
RUNTIME = Path(__file__).with_name("nip65_seed_runtime.cjs")


@pytest.mark.skipif(not NODE, reason="needs node to run the shipped client code")
def test_seeding_relays_never_turns_the_switch_on():
    done = subprocess.run([NODE, str(RUNTIME)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr or done.stdout
    assert "nip65 seed scenarios" in done.stdout, done.stdout


def _seeder() -> str:
    return APP[APP.index("  async function seedRelaysFromNip65(){"):APP.index("  function defaultRelays(){")]


def test_the_seeder_does_not_write_the_switch_at_all():
    """The rule as a rule, not as an outcome: this function may write the LIST and must not write
    the toggle. Expressed here too because the runtime can only test the paths it thinks of, and a
    new branch that sets it would be a new path."""
    body = _seeder()
    assert "ClientSettings.set('relays'" in body, "the seeder no longer adds what it found"
    assert "set('relaysEnabled'" not in body.replace(" ", ""), (
        "seedRelaysFromNip65 writes the 'use my own relays' switch again. Publishing a relay list "
        "is not the same as asking to use it instead of the node's — and an empty saved list is "
        "what somebody has right after switching it OFF.")


def test_the_only_writers_of_the_switch_are_controls_a_person_operates():
    """Every place that turns it on must be somewhere a person clicked. This is the whole family of
    the bug: the switch acquiring a value from an inference rather than an action."""
    # Each one verified by reading it: a person typed in a box and pressed something.
    allowed_markers = (
        # The sign-in Connection pane. Its box is PRE-FILLED with the server's defaults, so it
        # compares against the seed it put there and refuses to claim a choice it cannot prove.
        "_persistAuthRelays",
        "_saveAuthRelays",         # the same pane's explicit Save button
        # Settings → relays: the #set-relays-on checkbox and its Save. `on` is read from the
        # checkbox, so the value comes from the person, including when it is false.
        "renderUserSettings",
        "_dropLegacyAutoRelays",   # one-shot repair, and it only ever turns it OFF
    )
    writers = []
    for i, line in enumerate(APP.splitlines(), 1):
        if "set('relaysEnabled'" not in line.replace(" ", ""):
            continue
        # Which function is this in? Walk back to the nearest declaration.
        head = "\n".join(APP.splitlines()[:i])
        at = max(head.rfind("\n  function "), head.rfind("\n  async function "))
        name = head[at:].split("(")[0].split()[-1] if at > 0 else "?"
        writers.append((i, name, line.strip()[:80]))
    unknown = [w for w in writers if not any(m in w[1] for m in allowed_markers)]
    assert not unknown, (
        "these write the 'use my own relays' switch and are not a control a person operates — if "
        "one of them is legitimate, add it to allowed_markers with the reason: %r" % (unknown,))
