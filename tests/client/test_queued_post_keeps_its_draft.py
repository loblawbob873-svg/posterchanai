"""A post that is only QUEUED must keep the draft that is the last copy of what somebody wrote.

The composer dropped the draft on `r.queued` as well as `r.ok`, reasoning that a queued post is not
lost — it is in the timeline with a Pending badge, and that badge is where you discard it. That holds
only while the event can EVENTUALLY be accepted. An event the relay will refuse for ever (a wrongly
signed one, say) is queued, badged, retried and finally given up on, and by then the only copy of the
text is gone. That is how a quote post was lost, and "drafts is empty" is how it was reported.

The rule now: the draft survives until the event actually SENDS.

  keeps-on-queue   `r.queued` must not drop the draft
  maps-it          …and must record which draft belongs to that event id, or nothing can ever
                   clear it and every offline post leaves a stale draft behind
  clears-on-send   the flush clears the draft for anything that went out
  keeps-on-drop    …and deliberately does NOT for anything given up on: that copy is the only
                   place the text still exists

Read from the shipped app.js. This is a source test on purpose: the behaviour lives inside the
composer's submit handler, which needs the whole modal, the signer and a relay to run — and the rule
worth pinning is one line in each of the two composer paths.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


def _src():
    with open(APP) as fh:
        return fh.read()


class QueuedDraftTests(unittest.TestCase):

    def test_no_composer_path_drops_a_draft_for_a_merely_queued_post(self):
        src = _src()
        self.assertNotIn("if(r && (r.ok || r.queued)) _dropDraft();", src,
                         "a queued post still drops its draft — the text is gone if it is never "
                         "accepted")

    def test_every_queued_post_records_which_draft_is_its_own(self):
        """Without the mapping the draft can never be cleared, so the fix above would trade a lost
        post for a stale draft after every offline post. Both composer paths must do it."""
        src = _src()
        self.assertGreaterEqual(len(re.findall(r"_qDraftSet\(r\.ev\.id,", src)), 2,
                                "a composer path queues a post without recording its draft")

    def test_the_flush_clears_drafts_that_went_out_and_keeps_the_rest(self):
        """The other half of the same rule, and the half that makes keeping the draft safe."""
        src = _src()
        flush = src[src.index("function _flushOutbox()"):]
        flush = flush[:flush.index("\n  }")]
        self.assertIn("sentIds", flush, "the flush no longer clears drafts for what it sent")
        self.assertIn("Drafts.remove", flush)
        # The dropped branch takes the MAPPING (so a later event id cannot collide with a stale
        # entry) and must not remove the draft itself.
        drop = flush[flush.index("dropped.forEach"):]
        drop = drop[:drop.index("\n")]
        self.assertIn("_qDraftTake", drop)
        self.assertNotIn("Drafts.remove", drop,
                         "the recovery copy is deleted for an item that was given up on")


if __name__ == "__main__":
    unittest.main()
