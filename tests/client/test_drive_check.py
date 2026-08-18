"""Files → Blossom: does the drive actually hold what the index says it does?

The grid draws what the INDEX says you have; the server holds what is actually stored. Most of the
time those agree, and when they do not, nothing says so — a file whose bytes are gone looks entirely
normal until the day somebody opens it. That is the same shape as the folder-sync report that
prompted this ("Files → Blossom says this file has no stored copy"), one level up.

The rules that make the check safe rather than another way to lose data:

  * it is READ-ONLY. The one action it offers clears INDEX entries whose bytes are gone, which
    deletes nothing from the server — there is nothing there to delete.
  * it reads the server's list FRESH. `_blobHave` is whatever the last screen left behind, and on the
    drive home that is often nothing, which would report every file as missing.
  * blobs the index does not name are REPORTED, never offered for deletion. Folder sync, its records
    and the music library all keep their own bookkeeping and none of it appears in this index — an
    "orphan" here is usually somebody else's, and deleting it would be the wipe this app has already
    paid for twice.
  * an encrypted entry this device has no key for is a KEY problem, not a storage one, and saying so
    is the difference between "your file is gone" and "sign in on this device".
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


class DriveCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP, encoding="utf-8") as fh:
            cls.src = fh.read()
        at = cls.src.index("async function driveCheck(btn){")
        cls.body = cls.src[at:cls.src.index("\n  /* Does the server already hold", at)]

    def test_it_is_reachable_from_the_drive(self):
        self.assertIn('class="btn btn-ghost small fx-check"', self.src,
                      "there is no way to run a drive check")
        self.assertIn("driveCheck(cb)", self.src)

    def test_it_reads_the_servers_list_fresh(self):
        """`_blobHave` is whatever the last screen left behind — on the drive home, often nothing,
        which would report every file in the index as missing."""
        self.assertIn("/list/", self.body)
        self.assertIn("cache:'no-store'", self.body)
        # The comment explains why it does not use the cache, so strip comments before asserting —
        # a checker that reads its own postmortem as a call site cries wolf for ever.
        code = re.sub(r"/\*.*?\*/", "", self.body, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        self.assertNotIn("_blobHave", code,
                         "it judges the drive from a cache another screen filled in")

    def test_an_unreadable_listing_changes_nothing(self):
        self.assertIn("if(!Array.isArray(list))", self.body)
        self.assertIn("nothing was changed", self.body)

    def test_it_never_offers_to_delete_a_blob(self):
        """Folder sync, its records and the music library keep their own bookkeeping and none of it
        is in this index, so "not named here" is not "unreferenced"."""
        self.assertNotIn("deleteBlobQuiet", self.body)
        self.assertNotIn("delBlob", self.body)
        self.assertNotIn("method:'DELETE'", self.body)

    def test_the_only_repair_is_the_index_and_it_is_batched(self):
        repair = self.body[self.body.index("fx-ck-clear"):]
        self.assertIn("FilesIdx.beginBatch();", repair)
        self.assertIn("FilesIdx.forget(x.sha)", repair)
        self.assertIn("const saved = await FilesIdx.endBatch();", repair)

    def test_it_reports_what_the_save_did_not_what_it_asked(self):
        repair = self.body[self.body.index("fx-ck-clear"):]
        self.assertIn("const saved = await FilesIdx.endBatch();", repair,
                      "it claims the entries were cleared without checking the index was written")

    def test_a_key_problem_is_not_reported_as_a_missing_file(self):
        self.assertIn("undecryptable", self.body)
        self.assertIn("key problem, not a storage one", self.body)

    def test_blobs_the_index_does_not_name_are_explained_not_alarmed_about(self):
        self.assertIn("not named by this index", self.body)
        self.assertIn("Nothing to do", self.body)

    def test_it_names_the_server_it_asked(self):
        """`mediaServer()` is ONE current server and an index entry keeps no host of its own. An
        account that was ever pointed elsewhere has entries whose bytes are on the OLD host, and both
        the listing and the HEAD then answer 404 about a file that is perfectly safe. Naming the
        server is the only thing on the screen that makes that possible to spot."""
        self.assertIn("checked against", self.body)
        self.assertIn("mediaServer()", self.body)

    def test_a_doubted_file_is_reported_with_something_lookupable(self):
        """A name cannot be looked up anywhere — encrypted uploads never send the plaintext name to
        the server — so a report of names alone cannot be checked by anybody, including the person
        who wrote the check."""
        self.assertIn("x.sha.slice(0, 16)", self.body)

    def test_the_missing_list_is_not_dumped_whole_onto_the_screen(self):
        """A drive that has lost a thousand files must still produce a readable answer."""
        self.assertIn("reallyGone.slice(0, 12)", self.body)
        self.assertIn("more", self.body)


class DriveCheckRepairGuardTests(unittest.TestCase):
    """The repair TOMBSTONES entries, so it is not a local tidy — it is a delete on every device.

    `FilesIdx.forget()` records a tombstone, and the index merge strips tombstoned shas out of any
    copy this account pulls for the next ninety days. So "clear the dead entries" on the strength of
    one listing that answered `200 []` — a re-pointed instance, a node whose ownership rows were
    lost, a proxy with an opinion — would destroy the names, folders and encrypted-folder membership
    of a drive whose bytes are all still there.

    Three things stand between that and somebody's drive, and each is checked here.
    """

    @classmethod
    def setUpClass(cls):
        with open(APP, encoding="utf-8") as fh:
            src = fh.read()
        at = src.index("async function driveCheck(btn){")
        cls.body = src[at:src.index("\n  /* Does the server already hold", at)]

    def test_every_candidate_is_confirmed_against_the_server_itself(self):
        """One listing is one opinion. A HEAD per doubted entry is the second one, and an unknown
        answer counts as PRESENT."""
        self.assertIn("_blobPresent(x.sha)", self.body)
        self.assertIn("there === false", self.body,
                      "anything other than a definite 'not there' is treated as loss")
        self.assertIn("reallyGone", self.body)

    def test_the_button_offers_only_what_was_confirmed(self):
        self.assertIn("reallyGone.length ?", self.body)
        self.assertNotIn("Clear ${missing.length}", self.body)

    def test_it_refuses_to_clear_more_than_it_keeps(self):
        """The rule the phone book and folder sync both use, and for a stronger reason here."""
        repair = self.body[self.body.index("fx-ck-clear"):]
        self.assertIn("reallyGone.length >= 20 && reallyGone.length > keep", repair)
        self.assertIn("refused:", repair)

    def test_it_asks_before_it_clears(self):
        repair = self.body[self.body.index("fx-ck-clear"):]
        ask = repair.index("uiConfirm")
        do = repair.index("FilesIdx.forget(")
        self.assertLess(ask, do, "it clears the entries before asking")
        self.assertIn("every device", repair, "the confirmation does not say how far this reaches")

    def test_it_does_not_claim_the_index_is_unchanged_after_clearing(self):
        """The entries are out of the index and tombstoned by then, and the retry is armed — the same
        lie the music delete had, fixed the same way."""
        repair = self.body[self.body.index("fx-ck-clear"):]
        self.assertNotIn("your index on the server is unchanged", repair)
        self.assertIn("will retry", repair)


class TheProbeTests(unittest.TestCase):
    """"Is it there?" and "may I skip this upload?" are different questions.

    `_blobAlreadyStored` answers the second, and deliberately says NO for a blob that is present but
    carries an expiry stamp — re-uploading is what clears the stamp. Used as evidence of loss it
    reports files that are sitting on the server as gone, and this drives a repair that removes index
    entries on every device for ninety days. Reported from a real drive: 497 "missing" files with
    ordinary names.
    """

    @classmethod
    def setUpClass(cls):
        with open(APP, encoding="utf-8") as fh:
            cls.src = fh.read()
        at = cls.src.index("async function driveCheck(btn){")
        cls.body = cls.src[at:cls.src.index("\n  /* Does the server already hold", at)]

    def test_the_check_does_not_use_the_upload_shortcut_as_evidence(self):
        self.assertNotIn("_blobAlreadyStored", self.body,
                         "it judges loss with the question that means 'may I skip this upload'")
        self.assertIn("_blobPresent(x.sha)", self.body)

    def test_the_probe_distinguishes_absent_from_unanswerable(self):
        at = self.src.index("async function _blobPresent(sha){")
        p = self.src[at:self.src.index("\n  }", at)]
        self.assertIn("r.status === 404 || r.status === 410", p)
        self.assertIn("return null", p)
        self.assertIn("r.status === 200 || r.status === 206", p)

    def test_a_doubted_but_present_file_never_renders_an_empty_warning(self):
        """The listing doubts 500 entries; the HEADs find every one present. The old gate rendered
        "0 entries whose stored copy is gone:" over an empty list — this screen's own way of crying
        wolf — and skipped "Everything checks out" for a drive that had just checked out."""
        self.assertIn("vindicated", self.body)
        self.assertIn("reallyGone.length || unconfirmed", self.body,
                      "the warning is gated on what the listing doubted, not on what was confirmed")
        self.assertIn("!reallyGone.length && !unconfirmed && !unsure && !undecryptable.length",
                      self.body,
                      "'everything checks out' is claimed over entries nothing could answer for")

    def test_an_unanswerable_probe_is_reported_and_not_offered_for_deletion(self):
        self.assertIn("unsure++", self.body)
        self.assertIn("could not be checked a second time", self.body)
        # …and only the confirmed ones reach the button.
        self.assertIn("reallyGone.length ?", self.body)
