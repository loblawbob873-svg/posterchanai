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
class APartialLoadWritesButDoesNotDeleteTests(unittest.TestCase):
    """THE SECOND FAILURE, and it was caused by the fix for the first.

    Refusing the whole sweep whenever a load came back short traded "the phone book empties itself"
    for "nothing is ever written to the phone" — reported, verbatim, as *"nothing going to android
    contacts app"*. It is the quieter of the two and in some ways the worse one to diagnose: a
    per-book failure keeps the last good cards, so the Contacts screen shows a complete address book
    with NO error, the sweep says nothing at all, and if a book fails reliably the feature is dead
    for ever with nothing anywhere to say so.

    The rule the fix restores is an asymmetry, not a threshold: A SHORT LOAD MAY INSERT AND UPDATE,
    AND MAY NOT RECONCILE. Deleting is what a short keep-set gets catastrophically wrong; writing one
    cannot lose anybody, and the worst an extra row can do is wait for a whole sweep to remove it.
    """

    def test_a_partial_load_still_writes_every_contact_it_did_load(self):
        """FAILS BEFORE THE FIX: zero bridge calls, zero rows, zero toasts."""
        res = run(books=[BOOK, BOOK2], cards={"default": TEN, "work": TWO},
                  failBooks=["work"], phone=[])
        puts = [c[1] for c in res["calls"] if c[0] == "put"]
        self.assertTrue(puts, "a partial load wrote nothing to the phone at all")
        self.assertEqual(sorted(u for b in puts for u in b), sorted(c["uid"] for c in TEN))
        self.assertEqual(sorted(res["phoneRows"]), sorted(c["uid"] for c in TEN))

    def test_a_partial_load_never_reconciles(self):
        """The half that must NOT come back with it. Verified to fail if the prune is re-allowed:
        the two contacts in the book that did not load would be deleted from the phone."""
        res = run(books=[BOOK, BOOK2], cards={"default": TEN, "work": TWO},
                  failBooks=["work"], phone=ALL_TEN)
        self.assertEqual(commits(res), [], "a partial load was allowed to delete")
        self.assertEqual(sorted(res["phoneRows"]), sorted(ALL_TEN))

    def test_a_partial_load_says_which_half_it_skipped(self):
        """"Nothing was deleted" and "nothing ran" look identical from the phone."""
        res = run(books=[BOOK, BOOK2], cards={"default": TEN, "work": TWO},
                  failBooks=["work"], phone=[])
        self.assertIn("prune=skipped", res["diag"])
        self.assertIn("landed=8", res["diag"])

    def test_the_reconcile_comes_back_on_the_next_whole_load(self):
        """The mode is part of the push signature, so a sweep that wrote everything without being
        allowed to reconcile must not tell the next one there is nothing left to do."""
        res = run(books=[BOOK, BOOK2], cards={"default": TEN, "work": TWO}, phone=list(ALL_TEN),
                  steps=["failbooks:work", "reload", "settle", "ok", "reload", "settle"])
        self.assertEqual(len(commits(res)), 1, "the reconcile never resumed after a partial load")
        self.assertEqual(sorted(commits(res)[0]), sorted(ALL_TEN))


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TheSweepMustMeasureWhatItDidTests(unittest.TestCase):
    """A SWEEP THAT REPORTS SUCCESS AND WRITES NOTHING IS THE FAILURE MODE OF THIS FEATURE.

    There is no device on the machine this is developed on — no adb, no emulator — so every round of
    "here is a fix, install this APK" returns exactly one bit, and four builds were spent that way.
    What ends that is not a better guess: it is the sweep reporting numbers it MEASURED. `applyBatch`
    does not throw for an operation that changes nothing, so the row count under our account, read
    back after the write, is the only thing that can tell a sweep that worked from one that did not.
    """

    def test_a_provider_that_accepts_everything_and_stores_nothing_is_reported(self):
        res = run(books=[BOOK], cards={"default": TEN}, phone=[], putNoop=True)
        self.assertTrue(any("stored none of them" in t for t in res["toasts"]),
                        "the sweep reported success while the phone stayed empty")
        self.assertIn("landed=0", res["diag"])
        self.assertIn("phone 0→0", res["diag"])

    def test_a_phone_with_no_account_says_so_instead_of_going_quiet(self):
        """Every raw contact hangs off the PosterChan account. The plugin used to REJECT the call,
        which arrives here inside a `catch(_){ return; }` — i.e. as silence."""
        res = run(books=[BOOK], cards={"default": TEN}, phone=[], noAccount=True)
        self.assertTrue(any("account" in t for t in res["toasts"]),
                        "a phone that can hold nothing said nothing")
        self.assertEqual(res["phoneRows"], [])
        self.assertIn("no contacts account", res["diag"])

    def test_the_two_lines_of_the_panel_never_disagree_about_the_account(self):
        """THE CONTRADICTION THAT SOLVED THIS ONE, kept as a property.

        A handset printed `… no contacts account on this phone` and, directly beneath it, `phone:
        permission=yes account=yes rows=0` — seconds apart, about one phone. That is what proved the
        fault was in the sweep's account check rather than in permissions, the relay or the write
        (ContactWriter.ensureAccount was returning false because setIsSyncable threw for want of
        WRITE_SYNC_SETTINGS, while getAccountsByType said the account was plainly there).

        Both lines now come from the SAME measurement, so they can be stale together but they cannot
        contradict each other. Asserted in both directions — a test that only checked the broken
        phone would pass against a probe hardwired to `false`."""
        for no_account in (False, True):
            res = run(books=[BOOK], cards={"default": TEN}, phone=[], noAccount=no_account)
            said_none = "no contacts account" in res["diag"]
            self.assertEqual(said_none, no_account, res["diag"])
            # …and the probe line beneath it agrees, which is the whole point.
            self.assertEqual(res["probe"]["account"], not said_none,
                             f"the sweep said {said_none and 'no account' or 'account'} while the "
                             f"probe said account={res['probe']['account']}: {res['diag']}")

    def test_a_healthy_sweep_still_reports_its_numbers(self):
        """ON SUCCESS TOO. A diagnostic that only appears when something looks wrong would have said
        nothing about the build this exists for: nothing looked wrong."""
        res = run(books=[BOOK], cards={"default": TEN}, phone=[])
        self.assertIn("landed=8", res["diag"])
        self.assertIn("phone 0→8", res["diag"])
        self.assertIn("prune=ok", res["diag"])
        self.assertEqual(res["toasts"], [], "a healthy sweep must not interrupt anybody")

    def test_a_failed_write_is_retried_rather_than_signed_off(self):
        """Recording the push signature for a sweep that landed nothing tells every later sweep there
        is nothing left to try — the same shape as recording a hash for a batch the provider
        refused."""
        res = run(books=[BOOK], cards={"default": TEN}, phone=[], putNoop=True,
                  steps=["reload", "settle", "syncTick", "settle"])
        self.assertEqual(len([c for c in res["calls"] if c[0] == "put"]), 2,
                         "a write that landed nothing was recorded as done")
        # …and it is still said only once.
        self.assertEqual(len([t for t in res["toasts"] if "stored none" in t]), 1)


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
