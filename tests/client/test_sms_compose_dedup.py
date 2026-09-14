"""One tap must put one message on the carrier — including across a repaint.

The composer already coalesced repeated Enter/click while the first carrier promise was pending.
What it could not survive was a REPAINT: `let sending = false;` was declared inside the render, so
the latch existed only as long as that composer element did — and `paint()` runs on every incoming
message, receipt, contact refresh and relay event. A repaint landing mid-send built a fresh composer
with a fresh latch, still holding the typed text, with `S.attach` not yet cleared (it is cleared
only after the await returns). A second Enter then sent the same body and the same picture again.

Reported as "the picture I sent her, just sent twice".

The latch is module state keyed on the conversation, so no repaint can reset it and two different
threads can still be sent to at once. These assertions pin that SHAPE rather than one spelling: the
previous version matched the literal `let sending = false;` and broke the moment the bug was fixed,
while never having been able to catch the bug itself.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMS = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")


def _composer():
    start = SMS.index("const go = async () => {")
    end = SMS.index("/* `.bubble[data-doc]`", start)
    return SMS[start:end]


def _latch_scope():
    """The declaration of the latch, wherever it lives."""
    i = SMS.index("S.sending = S.sending || new Set();")
    return SMS[max(0, i - 1200):i + 200]


def test_the_latch_is_not_declared_inside_the_render():
    """A latch that a repaint can reset is not a latch.

    `paint()` fires on every incoming message; if the guard is re-created with it, a message arriving
    mid-send hands the user a fresh composer that will happily send the same thing again.
    """
    body = _composer()
    assert "let sending" not in body, (
        "the send guard is a local again — a repaint mid-send will reset it and the same message "
        "can go to the carrier twice")
    assert "S.sending" in body, "the send guard no longer lives on module state"


def test_the_latch_is_per_conversation():
    """Global would be correct for duplicates and wrong for people: two threads must not block."""
    scope = _latch_scope()
    assert "sendKey" in scope or "t.address" in scope, (
        "the guard is not keyed on the conversation, so sending to one person blocks sending to "
        "another")


def test_a_second_action_while_one_is_pending_does_nothing():
    body = _composer()
    assert "if(S.sending.has(sendKey)) return;" in body
    assert body.index("S.sending.add(sendKey);") < body.index("await send("), (
        "the guard is taken after the send starts, which is no guard at all")


def test_enter_does_not_submit_or_start_a_parallel_send():
    body = _composer()
    assert "if(e.key === 'Enter'){ e.preventDefault(); go(); }" in body


def test_a_failed_send_releases_the_guard_for_an_explicit_retry():
    body = _composer()
    finally_body = body.split("finally{", 1)[1]
    assert "S.sending.delete(sendKey);" in finally_body, (
        "a failed send leaves the conversation permanently unable to send")
    assert "btn.disabled = false;" in finally_body
