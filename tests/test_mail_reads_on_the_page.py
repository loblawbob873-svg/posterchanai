"""AN EMAIL IS READ ON THE PAGE, NOT THROUGH A PORTHOLE — and the sandbox stays closed.

Reported as "mobile UI leaves a small window to actually read the message".

HTML mail renders in a sandboxed iframe, which is correct: it is somebody else's markup. But an
iframe does not size to its content, so the stylesheet gave it a fixed box —
`height:calc(100dvh - 300px)`, and `62dvh` on a phone — with its own scrollbar inside the page's.
Measured on a 390x844 phone: a ~520px window onto the message.

It now measures the rendered document and sets the frame's height, so the page scrolls once and the
message is simply there.

THE SECURITY CONDITION, which is the whole reason this was not done sooner: reading the height needs
`allow-same-origin`. That is safe ONLY while `allow-scripts` is absent — with scripts off nothing
executes inside the frame, so it cannot use that origin for anything. Together they would be
untrusted email HTML running with our origin's privileges: access to the session and the key. This
file fails if they ever appear together, whatever else changes.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _iframe_tag() -> str:
    at = APP.index('<iframe class="mail-html"')
    return APP[at:APP.index("</iframe>", at)]


def test_the_mail_frame_never_gets_scripts():
    """THE LINE THAT MUST NOT MOVE. Untrusted mail HTML with our origin AND script execution is
    stored XSS against the session and the key."""
    tag = _iframe_tag()
    assert "allow-scripts" not in tag, (
        "the mail iframe grants allow-scripts — with allow-same-origin also present this is "
        "somebody else's email running as us")


def test_no_mail_iframe_anywhere_grants_both():
    """Stated over the whole file, not just the one tag, so a second mail frame cannot be added
    with the dangerous pair."""
    for tag in re.findall(r"<iframe[^>]*mail-html[^>]*>", APP):
        assert not ("allow-scripts" in tag and "allow-same-origin" in tag), tag


def test_the_frame_can_be_measured():
    """Without allow-same-origin the parent cannot read the height and the porthole comes back."""
    assert "allow-same-origin" in _iframe_tag()
    assert 'data-mail-autosize="1"' in _iframe_tag()


def test_something_actually_sizes_it():
    """The wiring, not the helper: a sizer nothing calls leaves the box exactly as it was."""
    assert "_sizeMailFrames(" in APP
    body = APP.split("_sizeMailFrames(root){", 1)[1].split("\n    },", 1)[0]
    assert "scrollHeight" in body and "style.height" in body
    thread = APP.split("_renderThread(pane, thread, folder, acct, seedUid){", 1)[1][:6000]
    assert "this._sizeMailFrames(pane)" in thread, (
        "the reading pane renders without ever sizing its frames")


def test_it_survives_a_frame_it_cannot_measure():
    """A frame that refuses measurement must fall back to the stylesheet, not throw and take the
    rest of the reading pane's bindings with it."""
    body = APP.split("_sizeMailFrames(root){", 1)[1].split("\n    },", 1)[0]
    assert "catch" in body


def test_a_single_message_body_is_not_stretched_over_its_measured_height():
    """`flex:1` on a singleton thread resolves flex-basis:0 and grows the item, which beats an
    inline height — the porthole would come straight back for the commonest case of all."""
    body = APP.split("_sizeMailFrames(root){", 1)[1].split("\n    },", 1)[0]
    assert "style.flex" in body, "a stretched single-message frame ignores its measured height"
    assert ".mail-thread>.mail-msg:first-child:nth-last-child(2) .mail-html{flex:1}" in CSS, (
        "this test's reasoning no longer matches the stylesheet — re-read it")


def test_the_phone_floor_is_no_longer_a_window():
    """62dvh was the porthole. What remains is only what an unmeasurable body falls back to."""
    assert "min-height:62dvh" not in CSS, "the phone still pins the message to a 62dvh box"
