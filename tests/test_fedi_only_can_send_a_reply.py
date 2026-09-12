"""Fediverse-only mode must be able to REPLY, which means it must accept kind 1111.

Run: venv-unified/bin/python -m unittest tests.test_fedi_only_can_send_a_reply

Reported as: "fediverse cannot send my reply to a fediverse post?" — the client refused before
signing with

    Fediverse-only mode cannot send this post type (kind 1111) — turn the mode off to post it.

which reads as one unusual post type being unsupported. It was every reply. `_commentScope` in
app.js gives a NIP-22 scope to a reply to an ORDINARY kind-1 note, so `replyKindFor` returns 1111
for all of them; the deliverable list — written when 1111 meant only polls, articles and live chat —
had never been revisited. Answering anybody at all was impossible in that mode.

Nothing was missing from the delivery path: `_handle` already has a `kind in (1, 1111)` branch whose
own comment says "1111 IS THE ORDINARY REPLY KIND NOW", `_is_reply`/`_reply_parent_id` both parse
NIP-22, and 1111 is in `_WRITEBACK_KINDS`. `tests/test_a_nip22_reply_reaches_the_fediverse.py`
covers all of that and passed throughout — because Fediverse-only is a SECOND route with its own
gate, and the rule was enforced in one copy of two.

So the rule under test is not "1111 is allowed". It is that the two lists, and the two surfaces that
read them, cannot disagree.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static/js/client/app.js"

from app.services import fedi_only_service


def _js_kind_set(name: str) -> set:
    src = APP_JS.read_text()
    m = re.search(re.escape(name) + r"\s*=\s*new Set\(\[([0-9,\s]*)\]\)", src)
    assert m, f"{name} is gone from app.js, or is no longer a literal Set"
    return {int(n) for n in m.group(1).split(",") if n.strip()}


class TheTwoSurfacesAgree(unittest.TestCase):
    def test_the_client_refuses_exactly_what_the_server_refuses(self):
        """A client list that is SHORT refuses a post the bridge would have delivered; one that is
        LONG signs a post that dies at the server. Neither is recoverable from the message."""
        self.assertEqual(_js_kind_set("_FEDI_DELIVERABLE_KINDS"),
                         set(fedi_only_service.SUPPORTED_KINDS),
                         "app.js and fedi_only_service.SUPPORTED_KINDS disagree about what "
                         "Fediverse-only mode can send")

    def test_a_reply_is_deliverable(self):
        self.assertIn(1111, fedi_only_service.SUPPORTED_KINDS,
                      "kind 1111 is the ordinary reply kind — without it, Fediverse-only mode "
                      "cannot answer anybody")
        self.assertIn(1111, _js_kind_set("_FEDI_DELIVERABLE_KINDS"))

    def test_every_deliverable_kind_is_one_the_bridge_handles(self):
        """The deliverable set must be a subset of what the writeback layer actually implements."""
        from app.services import fedi_nostr_writeback_service as wb
        handled = set(wb._WRITEBACK_KINDS) | {16}     # 16 is a generic repost, handled with 6
        extra = set(fedi_only_service.SUPPORTED_KINDS) - handled
        self.assertFalse(extra, f"Fediverse-only mode accepts {sorted(extra)}, which the writeback "
                                f"layer does not implement — those would be signed and dropped")


class ADeliveredReplyIsReportedAsDelivered(unittest.TestCase):
    def test_the_success_check_looks_in_the_table_the_reply_was_written_to(self):
        """The bug waiting behind the first one.

        `_handle`'s `kind in (1, 1111)` branch records a **FediBridgeDelivered** row. `route()` chose
        its table with `ev["kind"] == 1`, so a 1111 was looked up in FediBridgeAction, found nothing,
        and reported "Fediverse delivery failed. Nothing was published to Nostr." about a reply that
        HAD been posted — which invites the user to send it again.
        """
        import inspect
        src = inspect.getsource(fedi_only_service.route)
        self.assertIn("FediBridgeDelivered if ev[\"kind\"] in (1, 1111)", src,
                      "route() picks the delivery table by `kind == 1`, so a delivered NIP-22 reply "
                      "is reported as a failure")

    def test_a_comment_still_requires_a_bridged_parent(self):
        """1111 must not become a way to post a standalone status. A comment answers something, and
        if that something was never mirrored there is nothing on the fediverse to thread under."""
        import inspect
        src = inspect.getsource(fedi_only_service.route)
        self.assertIn('ev["kind"] != 1 or writeback._is_reply(ev)', src,
                      "the 'no Fediverse bridge target' guard no longer covers non-kind-1 posts")


if __name__ == "__main__":
    unittest.main()
