"""A removed torrent must not come back on the next restart.

Removal deletes the `.resume` file so the torrent is not re-added at start-up — but it only ever
deleted the ONE named by the torrent's dict key, and a resume file can be on disk under a different
name. `_stable_ih` documents why: an older build asked libtorrent for the deprecated v1 hash, which
answers all-zeros for a v2/hybrid torrent, so the file was written as `0000…0000.resume`. Loading
self-heals that name — but only AFTER re-adding the torrent it belongs to.

Measured in production (nas, 2026-08-08):

    20:30:29  [BT] REMOVED: 6543d5d07954d6f0c4396f71c6a18541b9df9748
    22:02:38  [BT] Renamed stale resume 0000…0000.resume -> 6543d5d0….resume
    22:02:38  [BT] Restored 1 torrents from resume data (0 running, 1 paused)
    22:02:38  [BT] ADDED: [Doomdos] …

— removed, and back an hour and a half later at the next deploy, so the user removed it twice.

These call the real `_purge_resume_files` against a real directory; libtorrent itself is not needed
to state the rule, which is that removal leaves NOTHING behind that a later glob could restore.
"""
import sys
import types
import unittest
from pathlib import Path
import tempfile
import shutil


def _service_class():
    """Import the module with libtorrent stubbed — it is a C extension pinned to one Python build
    (see the py-version skew note in the service), and none of it is needed to test file removal."""
    if "libtorrent" not in sys.modules:
        sys.modules["libtorrent"] = types.ModuleType("libtorrent")
    from app.services.libtorrent_service import LibtorrentService
    return LibtorrentService


class ResumePurge(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="btresume-"))
        cls = _service_class()
        # Only the two attributes _purge_resume_files touches — constructing the real service opens
        # a libtorrent session and a proxy connection.
        self.svc = cls.__new__(cls)
        self.svc.resume_dir = self.dir

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, body=b"unreadable-by-libtorrent"):
        p = self.dir / name
        p.write_bytes(body)
        return p

    def test_the_file_named_by_the_hash_is_deleted(self):
        ih = "6543d5d07954d6f0c4396f71c6a18541b9df9748"
        f = self._write(f"{ih}.resume")
        self.svc._purge_resume_files({ih})
        self.assertFalse(f.exists())

    def test_another_torrents_resume_file_is_left_alone(self):
        ih = "6543d5d07954d6f0c4396f71c6a18541b9df9748"
        mine = self._write(f"{ih}.resume")
        theirs = self._write("aa" * 20 + ".resume")
        self.svc._purge_resume_files({ih})
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists(), "only the removed torrent's file may be deleted")

    def test_the_all_zeros_name_is_deleted_too(self):
        """The production bug. The file is named by a hash the torrent no longer answers to, so a
        by-name delete misses it and the next start re-adds the torrent from it."""
        ih = "6543d5d07954d6f0c4396f71c6a18541b9df9748"
        zeros = self._write("0" * 40 + ".resume", body=self._resume_for(ih))
        self.svc._purge_resume_files({ih})
        self.assertFalse(zeros.exists(),
                         "a resume file whose CONTENT names this torrent must go, whatever it is called")

    def test_an_unreadable_stray_is_kept(self):
        """A file we cannot identify may belong to a torrent the user still has. Deleting that is
        the worse of the two mistakes, so an unparseable name+body is left in place."""
        stray = self._write("0" * 40 + ".resume", body=b"\x00\x01 not bencoded")
        self.svc._purge_resume_files({"6543d5d07954d6f0c4396f71c6a18541b9df9748"})
        self.assertTrue(stray.exists())

    def test_removal_purges_rather_than_unlinking_one_name(self):
        """The regression is a one-line revert away: `resume_dir / f"{info_hash}.resume"` deletes the
        name the torrent answers to TODAY and leaves the one an older build wrote."""
        src = (Path(__file__).resolve().parent.parent
               / "app" / "services" / "libtorrent_service.py").read_text()
        body = src[src.index("def _remove_locked"):src.index("def get_files")]
        self.assertIn("_purge_resume_files(forms)", body)
        self.assertNotIn('resume_dir / f"{info_hash}.resume"', body,
                         "removal is back to deleting a single name")

    def _resume_for(self, ih):
        """Enough of a resume file for the module's reader to answer with this info-hash."""
        import libtorrent as lt

        class _H:
            def __init__(self, s):
                self._s = s

            def is_all_zeros(self):
                return self._s == "0" * 40

            def __str__(self):
                return self._s

        class _Parsed:
            info_hashes = types.SimpleNamespace(v1=_H(ih), v2=_H("0" * 40))

        # The stub stands in for the C extension; a real libtorrent parses the same field.
        lt.read_resume_data = lambda _b: _Parsed()
        return b"d8:info-hash20:" + bytes.fromhex(ih) + b"e"


if __name__ == "__main__":
    unittest.main()
