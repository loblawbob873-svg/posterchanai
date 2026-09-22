"""A mail row that says "2" must open a thread containing 2.

Reported: "email from DAVO Customer Service says 2, but I can't click the first one" and "the
threading is broken". The badge comes from `_conversations()` — `count = all.length + mine.length`
— while the reader collects its own siblings in `openMsg` from `msgs.concat(convSent)` filtered by
`_convKey`. Those are two separate walks over the same data, and nothing made them agree.

This RUNS the shipped `_key`, `_convKey`, `_conversations` and the reader's sibling collection under
node over mailbox shapes that actually occur, and asserts the one invariant a user can see: what the
row counted is what opening it shows.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path
from tests.client_source import client_source

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


def _method(name):
    """Lift one method out of the MAIL object literal.

    Anchored on the mail object's own `_key`, not on the first match in the file — there is another
    `_key(m)` earlier (it reads ME) and lifting that one silently tests a different object.
    """
    src = client_source()
    anchor = src.index("_key(m){ return (m.account||this.acct)")
    start = src.index("\n    " + name + "(", anchor - 4000)
    i, depth, seen = src.index("{", start), 0, False
    while i < len(src):
        if src[i] == "{":
            depth += 1; seen = True
        elif src[i] == "}":
            depth -= 1
            if seen and depth == 0:
                return src[start:i + 1].strip().rstrip(",")
        i += 1
    raise AssertionError(name + " not found")


def _run(msgs, conv_sent):
    src = client_source()
    key = _method("_key")
    js = """
const M = {
  msgs: %s,
  convSent: %s,
  acct: 'me@x', folder: 'INBOX',
  %s,
  %s,
  %s,
};
const rows = M._conversations();
// What the reader collects for a row, the way openMsg does it.
const out = rows.map(c => {
  const seed = c.all[0] || c.head;
  const seedKey = M._convKey(seed);
  const local = [];
  for (const x of (M.msgs || []).concat(M.convSent || [])) {
    if (M._key(x) === M._key(seed)) continue;
    if (M._convKey(x) === seedKey) local.push(x);
  }
  local.push(seed);
  return { badge: c.count || c.all.length, opens: local.length,
           subject: (seed.subject || '') };
});
console.log(JSON.stringify(out));
""" % (json.dumps(msgs), json.dumps(conv_sent),
       key, _method("_convKey"), _method("_conversations"))
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr or "")[:600])
    return json.loads(p.stdout.strip().splitlines()[-1])


def _m(uid, subject, **kw):
    d = {"uid": uid, "subject": subject, "folder": "INBOX", "account": "me@x", "ts": uid, "read": True}
    d.update(kw)
    return d


class ConversationCount(unittest.TestCase):
    def _check(self, rows):
        for r in rows:
            self.assertEqual(r["badge"], r["opens"],
                             "the row for %r says %d but opening it shows %d"
                             % (r["subject"], r["badge"], r["opens"]))

    def test_two_received_in_one_conversation(self):
        self._check(_run([_m(2, "Your receipt"), _m(1, "Re: Your receipt")], []))

    def test_one_received_and_one_reply_of_mine(self):
        """The shape the report describes: a row that counts a sent reply it holds in `mine`."""
        self._check(_run([_m(2, "Your receipt")],
                         [_m(3, "Re: Your receipt", folder="Sent")]))

    def test_a_lone_message_says_one(self):
        rows = _run([_m(1, "Hello")], [])
        self.assertEqual(rows[0]["badge"], 1)
        self.assertEqual(rows[0]["opens"], 1)

    def test_subjectless_messages_do_not_merge(self):
        """`_convKey` falls back to the uid when there is no subject; two blanks are not a thread."""
        rows = _run([_m(1, ""), _m(2, "")], [])
        self.assertEqual(len(rows), 2, "two subjectless messages were merged into one conversation")
        self._check(rows)

    def test_several_of_mine_on_one_thread(self):
        self._check(_run([_m(1, "Invoice")],
                         [_m(2, "Re: Invoice", folder="Sent"), _m(3, "Re: Invoice", folder="Sent")]))

    def test_unified_view_across_accounts(self):
        """In All-inboxes the same subject can arrive at two accounts; a row must still add up."""
        self._check(_run([_m(1, "Newsletter", account="a@x"), _m(2, "Newsletter", account="b@x")], []))


if __name__ == "__main__":
    unittest.main()
