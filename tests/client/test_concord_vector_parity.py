"""Concord parity with Vector — the reference Concord client (its developers co-wrote the CORD specs).

Measured gaps, each a place a Vector user and ours did not see each other properly, or a thing
Vector could do in a room that we could not:

  * Vector replies with kind 9 + a NIP-C7 `q` tag — read as nothing, so every Vector reply arrived
    here as a message out of nowhere;
  * Vector notifies on `@npub1…` text only — our `@Name` + `p` tag pinged no Vector user;
  * our community icon was a plain `picture` URL — Vector (CORD-02 `icon` ImageRef) showed none;
  * our attachments were a PLAINTEXT imeta — readable on a public Blossom server;
  * no writer for edits, kicks, delegated bans, channel rename/delete, typing, roles, pins (the
    FIRST pin of a channel was refused outright) or dissolution — and a dissolution was never even
    looked for;
  * our rekey chunks were 120 rows (~99 KB), over strfry's 64 KB event limit;
  * NIP-59: the DM seal's signature was never verified on the remote-signer paths.

Both runtimes run the SHIPPED files under node: the reader writers against a real created community,
read back by the same fold every client uses; concord.js's functions against Vector-shaped events.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node required")


def _run(script, *markers):
    r = subprocess.run(["node", script], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:] or r.stdout[-2000:]
    for m in markers:
        assert m in r.stdout, (m, r.stdout[-1500:])


def test_the_writers_vector_has():
    _run("tests/client/concord_vector_parity_runtime.mjs", "roles passed", "authority citation passed",
         "delegated ban passed", "kick passed", "channel edit passed", "encrypted icon passed", "typing passed",
         "pins passed", "rekey chunking passed", "dissolution passed")


def test_what_a_vector_user_and_ours_see_of_each_other():
    _run("tests/client/concord_vector_interop_runtime.mjs", "replies passed", "mentions passed",
         "attachments passed", "edit gating passed", "nip17 seal passed")


def test_the_ui_is_wired_to_the_writers():
    """The runtimes prove the functions; this proves a person can reach them."""
    src = (ROOT / "static/js/client/concord.js").read_text()
    for needle in ("data-cc-edit=", "data-cc-pin=", 'id="cc-pins"', "data-cc-member-kick=", "data-cc-member-role=",
                   "data-cc-channel-more=", 'id="cc-dissolve"', 'id="cc-typing"', "data-cc-icon-pick=",
                   "void checkDissolution(p,room)", "sealAttachment(f)", "wireMentionText(text,mentionPairs,npubOf)",
                   "readableMentions(text)", "applyRoomBanner(room,info,loadKey)"):
        assert needle in src, needle
    live = src[src.index("function startChatLive("):]
    assert "{kinds:[21059],authors" in live[:4000], "the live subscription does not carry typing"
