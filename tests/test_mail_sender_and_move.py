"""Email: the sender is clickable, and Archive is one entry of a Move control.

Run: venv-unified/bin/python -m unittest tests.test_mail_sender_and_move

TWO THINGS A MAIL CLIENT HAS TO HAVE.

*Who sent this.* A mail header is `Some Name <someone@example.com>` rendered as one string, so the
ADDRESS — the part that says who it actually is — was visible only when the display name happened to
be missing. The sender is a button now, and every action behind it goes through machinery that
already exists (the composer, the mailbox search this screen is driven by) rather than a second path
that can disagree with the first. Three APIs were INVENTED in the first draft of that card —
`Mail.search()`, `PCContacts.all()`, `compose({to})` — and none of them existed; the buttons would
have thrown or silently done nothing. These tests pin what it is allowed to call.

*Moving a message.* Archive was a button of its own and "put this in the folder I keep receipts in"
was not possible at all. They are one control now, with Archive first — it is the common one, and
the only entry that may CREATE its destination.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text()
ROUTER = (ROOT / "app" / "routers" / "mail.py").read_text()
SERVICE = (ROOT / "app" / "services" / "mail_service.py").read_text()


def _mail_fn(name):
    i = APP.index("\n    " + name)
    return APP[i:APP.index("\n    },", i)]


class SenderCardTests(unittest.TestCase):
    def test_the_sender_is_a_button(self):
        self.assertIn('class="mm-sender"', APP)
        self.assertIn("data-from=", APP)
        self.assertIn("senderCard(b.dataset.from, b.dataset.name)", APP)

    def test_clicking_the_sender_does_not_collapse_the_message(self):
        """The header collapses the message and the sender lives inside it — without this, asking
        who sent something folds away what they wrote."""
        self.assertIn("if(e.target.closest('.mm-sender')) return;", APP)

    def test_it_shows_the_address_not_just_the_name(self):
        fn = _mail_fn("senderCard(email, name){")
        self.assertIn("rows.push(['Email'", fn)
        self.assertIn("replace(/\\s*<[^>]*>\\s*$/, '')", fn, "the display name still has the "
                                                             "<address> tail glued to it")

    def test_it_only_calls_things_that_exist(self):
        """Every one of these was invented in the first draft. `Mail.search()` and
        `PCContacts.all()` do not exist at all; `compose()` ignored a plain `to`."""
        fn = _mail_fn("senderCard(email, name){")
        for ghost in ("self.search(", "PCContacts.all", "PCContacts.newFrom"):
            self.assertNotIn(ghost, fn, f"{ghost} does not exist")
        self.assertIn("self.loadList();", fn, "'their messages' must drive the search this screen "
                                              "already runs")
        self.assertIn("self.q = addr;", fn)

    def test_write_to_them_actually_addresses_the_mail(self):
        """compose() only ever set a recipient for a reply or a forward, so `{to}` opened an empty
        composer — a button that looks like it worked."""
        self.assertIn("else if(opts.to) to=String(opts.to);", APP)
        fn = _mail_fn("senderCard(email, name){")
        self.assertIn("self.compose({ to: addr })", fn)


class MoveTests(unittest.TestCase):
    def test_archive_is_now_a_move_entry(self):
        self.assertIn('data-act="move"', APP)
        self.assertNotIn('data-act="archive">🗄 Archive</button>', APP,
                         "the standalone Archive button should have become the Move control")

    def test_the_folder_list_comes_from_the_server(self):
        """A picker that offers a mailbox the account does not have is a move that fails."""
        fn = APP[APP.index("      if(act==='move'){"):]
        fn = fn[:fn.index("\n      }")]
        self.assertIn("this.api('/folders?account='", fn)
        self.assertIn("{ v:'__archive', l:'🗄 Archive' }", fn)
        self.assertIn("f !== folder", fn, "it offers the folder the message is already in")

    def test_archive_keeps_its_own_endpoint(self):
        """Archive may CREATE its destination — that is what makes one press work on an account
        that has never had an Archive folder — and that is exactly what a user-chosen destination
        must not do."""
        fn = APP[APP.index("      if(act==='move'){"):]
        fn = fn[:fn.index("\n      }")]
        self.assertIn("this.api('/archive'", fn)
        self.assertIn("this.api('/move'", fn)

    def test_the_picker_dialog_exists(self):
        self.assertIn("function _pickOne(message, rows, opts={})", APP)
        _pick = APP[APP.index("function _pickOne"):][:1500]
        self.assertIn("uiconfirm-bg", _pick, "it must share uiConfirm's overlay — that is what "
                                             "keeps it above the desktop shell")
        self.assertIn("done(null)", _pick, "dismissing it must resolve, or the caller waits forever")


class ServerTests(unittest.TestCase):
    def test_the_endpoint_exists(self):
        self.assertIn('@router.post("/move")', ROUTER)
        self.assertIn("uid and dest are required", ROUTER)

    def test_it_reuses_the_move_that_was_already_there(self):
        """There was already a move_message — written for 'delete → Trash rather than expunge'. A
        second one was written before that was noticed; two functions with one name in one module is
        the later one silently winning."""
        self.assertEqual(len(re.findall(r"^def move_message\(", SERVICE, re.M)), 1)
        self.assertIn("move_message, current_user.id, db, acc.email, uid, folder, dest", ROUTER)

    def test_a_failed_move_is_reported(self):
        """Swallowed, it leaves the message where it was while the list has already removed it."""
        fn = ROUTER[ROUTER.index('@router.post("/move")'):]
        fn = fn[:fn.index("\ndef _decode_attachments")]
        self.assertIn("status_code=502", fn)
        self.assertIn("delete_message", fn, "the local mirror of the source must be dropped")

    def test_copy_before_delete(self):
        """The order is the safety property: \\Deleted before a confirmed COPY loses the message."""
        fn = SERVICE[SERVICE.index("def move_message("):]
        nxt = fn.find("\ndef ", 10)          # it is currently the LAST function in the module
        fn = fn[:nxt] if nxt > 0 else fn
        # The CALLS, not the prose: the docstring says "COPY + \\Deleted + EXPUNGE", so a plain
        # search for the word finds the sentence describing the order rather than the order.
        self.assertLess(fn.index('uid("COPY"'), fn.index('uid("STORE"'))
        self.assertLess(fn.index('uid("STORE"'), fn.index("expunge()"))


if __name__ == "__main__":
    unittest.main()
