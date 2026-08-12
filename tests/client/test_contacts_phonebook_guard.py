"""The phone book must never be emptied by a load that did not happen.

Run: venv-unified/bin/python -m unittest tests.client.test_contacts_phonebook_guard

`commit({uids})` is a KEEP-SET: everything under the PosterChan account that is not in it is deleted
from ContactsContract — out of the dialer, the share sheet, favourites, ringtones and shortcuts. So
an empty list is the most destructive thing this bridge can be handed, and every way to produce one
is silent:

  * the app opens before wifi associates and `/api/contacts/books` throws — `S.books` is still `[]`,
    every downstream reader sees "this account has no contacts", and the sweep at the end of load()
    reconciles that against the phone;
  * the node answers 200 with `{"books": []}` (a relay that read empty, contacts turned off
    mid-session) — the load "succeeds" and the same thing happens with no error anywhere.

This is the shape the repo already guards for the drive index and the folder-sync manifest. Unlike
those, the loss here is on somebody's PHONE, where this app is not the only thing reading it.

These run the shipped contacts.js under node against a stub ContactSync plugin (contacts_sim.js) —
a grep could not see the relationship between load() failing and commit() firing.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests" / "client" / "contacts_sim.js"

BOOK = {"id": "default", "displayname": "Contacts"}


def card(uid, name):
    return {"uid": uid,
            "ics": f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{name}\r\n"
                   f"TEL;TYPE=cell:555000\r\nEND:VCARD\r\n"}


def run(**opts):
    opts.setdefault("owner", "me")
    opts.setdefault("settings", {"androidPhonebook": True, "androidPhonebookOwner": "me"})
    out = subprocess.run(["node", str(SIM), json.dumps(opts)],
                         capture_output=True, timeout=90)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-3000:])
    return json.loads(out.stdout.decode())


def commits(res):
    return [c[1] for c in res["calls"] if c[0] == "commit"]


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PhonebookGuardTests(unittest.TestCase):

    def test_a_load_that_never_succeeded_does_not_reconcile_anything(self):
        """THE BUG. load() assigned books/cards only on success but swept unconditionally, so a
        failed first load handed the phone an empty keep-set and prune() deleted the lot."""
        res = run(failLoad=True, phone=["a", "b", "c"])
        self.assertEqual(commits(res), [], "a failed load must not reconcile the phone book")
        self.assertEqual(res["phoneRows"], ["a", "b", "c"], "the phone book was wiped")

    def test_a_load_that_fails_after_a_good_one_pushes_the_last_good_state(self):
        """The opposite mistake: refusing for ever after one blip. A later failure leaves the last
        successfully-loaded cards in place, and those are real state worth keeping in step."""
        res = run(books=[BOOK], cards={"default": [card("a", "Ann"), card("b", "Bo")]},
                  phone=["a", "b"], steps=["reload", "fail", "syncTick", "settle"])
        self.assertTrue(commits(res), "a sweep after a good load must still reconcile")
        self.assertEqual(sorted(commits(res)[-1]), ["a", "b"])
        self.assertEqual(sorted(res["phoneRows"]), ["a", "b"])

    def test_an_empty_load_never_empties_a_phone_that_holds_people(self):
        """A 200 carrying no books is not a report that this account has no contacts — it is the
        same empty read the drive index and the sync manifest are guarded against."""
        res = run(books=[], phone=["a", "b", "c"])
        self.assertEqual(commits(res), [], "an empty keep-set must be refused, not sent")
        self.assertEqual(res["phoneRows"], ["a", "b", "c"])
        self.assertTrue(any("left alone" in t for t in res["toasts"]),
                        "a refused reconcile must say so, not fail silently")

    def test_a_refusal_is_said_once_not_on_every_sweep(self):
        res = run(books=[], phone=["a"], steps=["reload", "syncTick", "syncTick", "settle"])
        self.assertEqual(len([t for t in res["toasts"] if "left alone" in t]), 1)

    def test_an_ordinary_delete_still_reaches_the_phone(self):
        """The guard is about an EMPTY keep-set. Removing one person of several must still prune."""
        res = run(books=[BOOK], cards={"default": [card("a", "Ann")]}, phone=["a", "b"])
        self.assertEqual(commits(res), [["a"]])
        self.assertEqual(res["phoneRows"], ["a"])

    def test_a_phone_with_nothing_on_it_yet_is_not_a_collapse(self):
        """First run of a brand-new account: zero cards, zero rows. Nothing to protect, and the
        reconcile has to run or the hash bookkeeping never starts."""
        res = run(books=[BOOK], cards={"default": []}, phone=[])
        self.assertEqual(commits(res), [[]])


BOOK2 = {"id": "work", "displayname": "Work"}
TEN = [card(u, u.upper()) for u in "abcdefgh"]          # eight in the first book…
TWO = [card(u, u.upper()) for u in "ij"]                # …two in the second
ALL_TEN = [c["uid"] for c in TEN + TWO]


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class AShortKeepSetTests(unittest.TestCase):
    """THE ONE THAT EMPTIED A REAL PHONE, TWICE — and the reason the reconcile was switched off.

    Both guards that existed then only ever asked "is the list EMPTY?", and it never was. A keep-set
    that is merely SHORT is the same delete order with a quieter symptom, and every way of producing
    one is silent: a per-book fetch that failed and was swallowed into `[]`, a relay read behind it
    that answered a 200 with fewer cards than the user has, a phone whose rows and the app's uids
    disagree about identity.
    """

    def test_a_book_that_did_not_load_never_shortens_the_keep_set(self):
        """THE BUG. `/api/contacts/books` answers, one book's cards do not, the failure is swallowed,
        and `loadedOk` — which is about history, and a load HAD completed — cannot see it. The sweep
        that follows says "these eight are all my contacts" to a phone holding ten."""
        res = run(books=[BOOK, BOOK2], cards={"default": TEN, "work": TWO}, phone=ALL_TEN,
                  steps=["reload", "settle", "failbooks:work", "reload", "settle"])
        self.assertEqual(len(commits(res)), 1, "a partial load reconciled the phone book")
        self.assertEqual(sorted(res["phoneRows"]), sorted(ALL_TEN),
                         "the two contacts in the book that did not load were deleted from the phone")

    def test_the_sweep_comes_back_when_the_whole_book_does(self):
        """The other half: refusing for ever after one blip is its own failure. A later whole load
        reconciles again, or a contact deleted in the web UI never leaves the phone."""
        res = run(books=[BOOK, BOOK2], cards={"default": TEN, "work": TWO}, phone=ALL_TEN,
                  steps=["reload", "settle", "failbooks:work", "remove:work:j", "reload", "settle",
                         "ok", "reload", "settle"])
        self.assertEqual(len(commits(res)), 2, "the sweep never resumed")
        self.assertEqual(sorted(commits(res)[-1]), sorted(u for u in ALL_TEN if u != "j"))
        self.assertNotIn("j", res["phoneRows"], "a real deletion never reached the phone")

    def test_a_short_list_is_refused_before_it_reaches_the_phone_at_all(self):
        """The client's own guard, which is what makes the refusal SAYABLE: no commit is attempted,
        so the phone is never asked to delete nine people and no toast has to explain a bridge call
        that half-happened."""
        res = run(books=[BOOK], cards={"default": [card("a", "Ann")]}, phone=ALL_TEN)
        self.assertEqual(commits(res), [], "a keep-set of 1 against 10 rows was sent to the phone")
        self.assertEqual(sorted(res["phoneRows"]), sorted(ALL_TEN))
        self.assertTrue(any("came back short" in t for t in res["toasts"]),
                        "a refused reconcile must say so, not fail silently")

    def test_the_plugin_refuses_a_collapse_the_client_cannot_see(self):
        """WHY THE NATIVE GUARD IS THE LOAD-BEARING ONE. The client compares two COUNTS, so a phone
        whose rows carry uids the app has never heard of — the identity mismatch the reconcile was
        switched off for — looks like an ordinary sweep from here: five stale rows, three cards, two
        more deleted than kept and nothing in the arithmetic to show it. The plugin compares the rows
        themselves, and it is the caller it does not trust.

        Both halves are asserted here, so this cannot pass against a plugin that does not guard."""
        cards = [card(u, u.upper()) for u in "abc"]
        stale = ["x", "y", "z", "w", "v"]
        res = run(books=[BOOK], cards={"default": cards}, phone=list(stale))
        self.assertTrue(commits(res), "the client refused it — this test needs it to get through")
        self.assertEqual(sorted(res["phoneRows"]), sorted(stale + ["a", "b", "c"]),
                         "the plugin obeyed a reconcile that deleted more than it kept")
        self.assertTrue(any("kept its" in t for t in res["toasts"]),
                        "the refusal never reached the user")

        # …and the same sweep against the phone as it was before the guard existed.
        was = run(books=[BOOK], cards={"default": cards}, phone=list(stale), nativeGuard=False)
        self.assertEqual(sorted(was["phoneRows"]), ["a", "b", "c"],
                         "unguarded, this sweep is meant to delete those five rows")

    def test_a_native_refusal_is_said_once_not_on_every_sweep(self):
        cards = [card(u, u.upper()) for u in "abc"]
        res = run(books=[BOOK], cards={"default": cards}, phone=["x", "y", "z", "w", "v"],
                  steps=["reload", "syncTick", "syncTick", "settle"])
        self.assertEqual(len([t for t in res["toasts"] if "kept its" in t]), 1)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ComingBackToTheScreenTests(unittest.TestCase):
    """A load that failed once must not be the answer for the rest of the page.

    `ready` was set on the way out of load() whatever happened, and render() only loads when
    `!ready`. So one blip — the app opening before wifi associates, a 502 while the node restarts —
    pinned Contacts to "could not load your contacts" until a full page reload, on a screen people
    reach from the sidebar several times a session.
    """

    def test_a_later_visit_retries_a_load_that_failed(self):
        res = run(failLoad=True, books=[BOOK], cards={"default": [card("a", "Ann")]},
                  phone=[], steps=["reload", "ok", "render"])
        self.assertEqual(len([u for u in res["fetched"] if "/books" in u]), 2,
                         "the screen never asked again — the error is pinned for the page")
        # …and the retry completed, so the sweep it ends in is armed again.
        self.assertTrue(any("/cards" in u for u in res["fetched"]))

    def test_a_successful_load_is_not_repeated_on_every_visit(self):
        """The other half: state lives in the module precisely so coming back is free."""
        res = run(books=[BOOK], cards={"default": [card("a", "Ann")]}, phone=["a"],
                  steps=["reload", "render", "render"])
        self.assertEqual(len([u for u in res["fetched"] if "/books" in u]), 1)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ConsentIsPerAccountTests(unittest.TestCase):
    """The phone-book switch used to survive sign-out.

    ClientSettings is DEVICE-wide and `Session.clear()` does not touch it, so a switch left on was
    consent the NEXT account inherited: sign in on that handset and their contacts were pushed into
    ContactsContract with no prompt and no opt-in. The plugin's own owner guard wipes the previous
    account's rows, which is the leak half — this is the other half, where somebody's address book
    is published to a phone they never agreed to.
    """

    def test_signing_out_turns_the_switch_off(self):
        res = run(phone=["a"], steps=["forget"])
        self.assertIs(res["settings"]["androidPhonebook"], False)
        self.assertEqual(res["settings"].get("androidPhonebookOwner", ""), "")
        self.assertIn(["disable"], res["calls"], "the device copy must be removed too")

    def test_another_accounts_consent_does_not_sync_this_one(self):
        res = run(owner="me", books=[BOOK], cards={"default": [card("a", "Ann")]}, phone=["x"],
                  settings={"androidPhonebook": True, "androidPhonebookOwner": "somebody-else"})
        self.assertEqual(res["calls"], [], "another account's switch synced this account's contacts")
        self.assertEqual(res["phoneRows"], ["x"])

    def test_a_device_that_had_it_on_before_consent_was_scoped_keeps_working(self):
        """Upgrade path. No owner recorded means the account signed in now is the one that turned it
        on — refusing there would silently stop syncing a phone book that already works."""
        res = run(owner="me", books=[BOOK], cards={"default": [card("a", "Ann")]}, phone=["a"],
                  settings={"androidPhonebook": True})
        self.assertEqual(commits(res), [["a"]])
        self.assertEqual(res["settings"]["androidPhonebookOwner"], "me",
                         "the owner must be recorded, or the next account inherits it again")


if __name__ == "__main__":
    unittest.main()
