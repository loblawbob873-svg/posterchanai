"""Replying to a message must look the original up in the mailbox it actually lives in.

Reported as "trying to send email is getting send failed just now". The server log said exactly what
happened:

    GET  /api/mail/message?account=verita84%40poster.place&folder=INBOX.Archive&uid=1813  -> 200 OK
    POST /api/mail/reply
         get_message_by_id: account=yummy@yummythai.restaurant, uid=1813, folder=INBOX.Archive
         ERROR: Original message not found: yummy@yummythai.restaurant/1813

The message was read from one account and the reply searched another. `/reply` uses a single
`account` for two different jobs — the identity the mail is sent AS, and the mailbox the original is
resolved in by uid+folder — and in the unified "All accounts" view the composer had no message-aware
answer for the second one: `this.acct` is '__all', so it fell through to `accounts[0]`, whichever
mailbox happens to sort first. The uid resolves to nothing there and the composer reports "send
failed" without ever saying which mailbox it searched.

The comment above that line already said "the message's account for reply/forward/draft". The code
never consulted `m.account`, which exists and is printed on every row of the unified list. This is
the comment being made true.
"""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js").read_text(
    encoding="utf-8")


def _from_acct_block():
    start = APP.index("const msgAcct = (m && m.account)")
    return APP[start:APP.index("const fromSel", start)]


class ReplyResolvesAgainstTheMessagesAccount(unittest.TestCase):
    def test_the_message_account_is_consulted_at_all(self):
        self.assertIn("m.account", _from_acct_block(),
                      "the composer picks an account without looking at the message it is "
                      "answering, so a reply in the unified view searches the wrong mailbox")

    def test_answering_prefers_it_over_every_fallback(self):
        """It must win over `accounts[0]`, which is the value that produced the reported failure."""
        block = _from_acct_block()
        self.assertRegex(block, r"answering\s*&&\s*msgAcct\s*\)\s*\?\s*msgAcct",
                         "the message's own mailbox is not the first choice when answering: %s"
                         % block[-300:])
        self.assertLess(block.index("msgAcct"), block.index("accounts[0]"),
                        "accounts[0] is still reached before the message's own account")

    def test_reply_forward_and_replyall_all_count_as_answering(self):
        block = _from_acct_block()
        for mode in ("'reply'", "'replyall'", "'forward'"):
            self.assertIn(mode, block, "%s does not resolve against the message's mailbox" % mode)

    def test_a_new_message_still_follows_the_from_selector(self):
        """Choosing an identity is what the From selector is for; this fix must not take it away."""
        self.assertIn("const sendAcct=()=>{ const s=$('#cm-from'); return (s&&s.value)||fromAcct; };",
                      APP, "the From selector no longer decides who a NEW message is sent as")

    def test_the_reply_payload_still_carries_uid_and_folder(self):
        """The account is only half the lookup; without these the server cannot find it either."""
        send = APP[APP.index("let path='/send';"):]
        send = send[:send.index("const btn=$('#cm-send')")]
        self.assertIn("payload.uid=m.uid", send)
        self.assertIn("payload.folder=opts.folder", send)


if __name__ == "__main__":
    unittest.main()
