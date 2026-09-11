"""WHY A REPLY DID OR DID NOT REACH THE FEDIVERSE HAS TO BE IN THE LOG.

Reported as "why are my replies not getting sent over the fediverse bridge for a fedi discussion".

Measured, and the bridge was fine: every reply in 24h whose parent was a bridged fediverse note
federated — public, threaded under the right parent, with the parent's author addressed. The ones
that did not federate were replies to NATIVE Nostr notes, where there is no fediverse post to answer.

BUT NEITHER OUTCOME COULD BE READ FROM THE LOG, and that is the actual defect:

  * the silent drop printed NOTHING. A reply whose parent was never mirrored simply returned, so
    "my replies aren't sending" had no answer anywhere and the only way to tell a working bridge
    from a broken one was to query the database by hand.

  * the success line named the WRONG OBJECT. It logged `target_id` — the PARENT's status id — which
    reads exactly like the id of the thing just created. Diagnosing this report with it, those
    statuses came back `in_reply_to=None, mentions=[]` (they are thread roots, of course) and the
    bridge looked broken when it was not. A log line naming the wrong object is worse than none.

  * two failures in the live log printed nothing after their colon — `%s` of an exception whose
    message is empty. A failure that does not say what failed is a silent one.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "app/services/fedi_nostr_writeback_service.py").read_text(encoding="utf-8")


def test_a_reply_that_stays_on_nostr_says_so():
    at = SRC.index("async def _handle(db, ev: dict")
    body = SRC[at:SRC.index("    inst, token = user.pleroma_instance_url", at)]
    assert body.strip(), "the _handle slice is empty — re-point this test"
    assert "not federated" in body, (
        "a reply whose parent is not a bridged note still returns silently; that question has no "
        "answer in any log")
    assert "_is_reply(ev)" in body and "logger.debug" in body, (
        "the explanation must be scoped to replies and must not be a warning — answering a native "
        "Nostr note is the ordinary case, not a fault")


def test_the_success_line_names_our_own_status_not_only_the_parent():
    assert "posted as" in SRC, (
        "the writeback log names only the parent again, which reads as the id of the status just "
        "created and sent this very diagnosis down the wrong path")
    assert 'posted_id = ""' in SRC, "posted_id is no longer initialised before the dispatch"
    assert "locals()" not in SRC, (
        "the log line reads the frame instead of a variable; that breaks the moment the branch moves")


def test_a_failure_names_its_exception_type():
    """Real lines from the live log: `action failed (kind 7, ev 181b…):` and nothing after the
    colon, because the exception's message was empty."""
    for marker in ("action failed", "cross-post failed"):
        at = SRC.index(marker)
        line = SRC[at:SRC.index("\n", SRC.index("logger.warning", at - 400))+400]
        assert "type(e).__name__" in line, f"{marker!r} can still print an empty reason"
        assert "(no message)" in line, f"{marker!r} has no fallback for an empty exception message"
