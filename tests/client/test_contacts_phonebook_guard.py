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


if __name__ == "__main__":
    unittest.main()
