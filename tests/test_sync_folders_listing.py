"""A forgotten folder disappears from the list — the OUTCOME, not the mechanism.

This exists because of how its gap was found. "Forget this folder" was built, tested (it wipes the
document), shipped — and the folder was still in Synced Folders afterwards, because this listing
enumerates the EVENTS and an emptied document is still an event. The test asserted what the code did
instead of what the person would see, which is the same mistake that let a missing checksum and a
mid-sweep loop reach a user on the same day.

The distinction that has to hold, and it is NOT "n == 0":

  * a manifest with NO ENTRIES is not a folder. It is what forget leaves behind, and listing it means
    the folder can never be removed — pressing forget again clears nothing, because nothing is left.
  * a manifest whose entries are ALL TOMBSTONES *is* a folder, with n = 0. It is how a device learns
    those files were deleted; dropping it would make a deletion unlearnable by any device that had
    not already seen it.
"""
import unittest

from app.routers.client import _sync_folder_rows


class ListingTests(unittest.TestCase):
    def test_an_emptied_record_is_not_listed(self):
        """What forget leaves behind. Listed, the folder can never be removed."""
        rows = _sync_folder_rows({"pcai:sync:Pictures": ({"paths": {}, "n": 0}, 100)})
        self.assertEqual(rows, [], "a forgotten folder is still in the list")

    def test_an_emptied_record_with_no_paths_key_is_not_listed_either(self):
        """`store.save` writes `{manifest: {}}`; what lands in the document is an implementation
        detail, so both shapes have to be treated the same."""
        self.assertEqual(_sync_folder_rows({"pcai:sync:Gone": ({"paths": {}}, 1)}), [])

    def test_a_folder_of_only_tombstones_IS_still_listed(self):
        rows = _sync_folder_rows({"pcai:sync:Pictures": ({"paths": {"a.jpg": {"deletedAt": 5}}}, 100)})
        self.assertEqual([r["key"] for r in rows], ["Pictures"])
        self.assertEqual(rows[0]["n"], 0, "a tombstoned folder has no LIVE files")

    def test_an_ordinary_folder_is_listed_with_its_live_count(self):
        rows = _sync_folder_rows({"pcai:sync:Docs": ({"paths": {"a": {"size": 1},
                                                                "b": {"deletedAt": 9}}}, 7)})
        self.assertEqual([r["key"] for r in rows], ["Docs"])
        self.assertEqual(rows[0]["n"], 1, "the count must be LIVE files, not entries")

    def test_a_sealed_manifest_is_listed_on_its_own_count(self):
        """The paths are NIP-44 sealed, so the server cannot read them — `n` beside the seal is the
        only number it has, and a sealed folder must not be dropped for having no readable paths."""
        rows = _sync_folder_rows({"pcai:sync:Vault": ({"sealed": "…", "n": 42}, 3)})
        self.assertEqual([r["key"] for r in rows], ["Vault"])
        self.assertEqual(rows[0]["n"], 42)

    def test_a_SEALED_wipe_is_not_listed_because_it_says_how_many_entries_it_has(self):
        """WHAT FORGET ACTUALLY PRODUCES, and the case the first version of this filter could not
        see. An empty manifest is two bytes, so it is sealed INLINE — the document is
        `{n: 0, entries: 0, sealed: ...}` with no `paths` key at all. Without the plaintext `entries`
        count the server cannot tell it from a folder whose files were all deleted, so the folder
        stayed in the list for ever and forget looked broken even once it worked."""
        rows = _sync_folder_rows({"pcai:sync:Pictures": ({"sealed": "x", "n": 0, "entries": 0}, 9)})
        self.assertEqual(rows, [], "a forgotten folder is still listed")

    def test_a_SEALED_record_that_HAS_entries_is_listed(self):
        """The other half: sealed, no live files, but tombstones inside. It is a real folder and its
        tombstones are how another device learns those files are gone."""
        rows = _sync_folder_rows({"pcai:sync:Old": ({"sealed": "x", "n": 0, "entries": 812}, 9)})
        self.assertEqual([r["key"] for r in rows], ["Old"])

    def test_a_SEALED_record_reporting_zero_is_still_listed(self):
        """THE CONSERVATIVE HALF, and my first version of this test had it backwards.

        Inside a seal the server cannot tell "no entries at all" from "every entry is a tombstone" —
        both report n = 0. Dropping it would hide a real folder whose files were deleted, which is
        precisely what the tombstone test above forbids. So emptiness must be VISIBLE to be acted on:
        `paths` present and empty, which is what forget actually writes (an empty manifest is far
        below the size that gets sealed)."""
        rows = _sync_folder_rows({"pcai:sync:Old": ({"sealed": "x", "n": 0}, 3)})
        self.assertEqual([r["key"] for r in rows], ["Old"],
                         "an OLDER client's sealed manifest carries no `entries`, and dropping it on "
                         "a count the server cannot verify would lose a real folder")

    def test_folders_come_back_in_a_stable_order(self):
        rows = _sync_folder_rows({
            "pcai:sync:zeta": ({"paths": {"a": {"size": 1}}}, 1),
            "pcai:sync:Alpha": ({"paths": {"a": {"size": 1}}}, 2),
        })
        self.assertEqual([r["key"] for r in rows], ["Alpha", "zeta"])
