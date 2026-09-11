"""A KIND WE SIGN BUT NEVER ASK PERMISSION FOR IS A FEATURE THAT SILENTLY CANNOT BE USED.

"why is my damn draft not signing!" — a fediverse reply, with PosterChan's own signer.

The pairing URI carries the permissions the client asks for, as `sign_event:<kind>`. OUR signer
enforces that list exactly (`allowed()` asks `g.indexOf('sign_event:' + kind)`), so a kind missing
from it is answered "no permission": no prompt, no error the person can act on, and no way to grant
it afterwards. Amber happens to prompt per-action and papers over the gap; ours, and iOS signers
like Clave, do not — the comment above the list has said so all along.

Replies became NIP-22 comments (kind 1111 via `replyKindFor`) and 1111 was never added, so every
reply to an ordinary note was refused by the signer the user had already paired.

THE LIST IS READ OUT OF THE CODE HERE rather than remembered. It grew from 16 kinds to 35 the first
time this ran, which is the measure of how well remembering was working.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "static/js/client"
APP = (CLIENT / "app.js").read_text(encoding="utf-8")

#: Kinds signed somewhere other than a literal `publish(<n>` / `sign(<n>` call — a variable kind, a
#: helper, or a constant. Each needs a reason, because the alternative is a silent "no permission".
EXTRA = {
    1059: "NIP-17 gift wrap: built by the signer's own nip17wrap, never a literal publish() call",
    22242: "NIP-42 relay AUTH: signed by the relay layer, not by publish()",
    27235: "NIP-98 HTTP auth: signed for git writes and the push endpoints",
    10003: "bookmark list, edited through _editEList rather than a literal publish()",
}


def _granted() -> set[int]:
    """The kinds the pairing URI asks for."""
    block = APP[APP.index("const kinds=["):]
    block = block[:block.index("]")]
    return {int(n) for n in re.findall(r"\d+", block)}


def _signed() -> set[int]:
    """Kinds this client signs, read from its own publish()/sign() calls."""
    out: set[int] = set()
    for path in sorted(CLIENT.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        out |= {int(m) for m in re.findall(r"(?:publish|sign)\(\s*(\d{1,5})\b", src)}
    return out


def test_both_lists_parse():
    """The check before the check: an empty parse makes the rule below pass about nothing."""
    assert len(_granted()) >= 16, "the pairing permission list no longer parses"
    assert len(_signed()) >= 20, "no signed kinds found — the scan is broken, not the code"


def test_every_kind_we_sign_is_asked_for():
    missing = sorted(_signed() - _granted())
    assert not missing, (
        "these kinds are signed by the client and are NOT in the pairing permission list, so our "
        "own signer answers 'no permission' with no prompt: %s. Add them to `kinds` in app.js."
        % ", ".join(str(k) for k in missing))


def test_the_reply_kind_is_granted():
    """Named on its own because it is the one that broke, and because every reply depends on it."""
    assert 1111 in _granted(), (
        "kind 1111 is not granted. Every reply to an ordinary note is a NIP-22 comment, so no reply "
        "can be signed at all")


def test_nothing_is_granted_that_is_never_signed():
    """The reverse, so the list stays a statement about this client rather than a wishlist. A grant
    is a capability handed to whatever holds the key; unused ones should be justified, not habitual."""
    unexplained = sorted(_granted() - _signed() - set(EXTRA))
    assert not unexplained, (
        "these kinds are asked for and never signed: %s. Remove them, or add the reason to EXTRA."
        % ", ".join(str(k) for k in unexplained))


def test_the_signer_still_checks_per_kind():
    """The rule this whole file depends on. If the signer ever accepts a bare `sign_event` grant for
    everything, the list stops mattering — and so does this test, silently."""
    assert "'sign_event:' + kind" in APP, (
        "the signer no longer authorises per kind; the permission list is now decorative")
