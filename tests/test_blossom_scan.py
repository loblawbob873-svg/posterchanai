"""Does this node hold what its database says it holds? — the scan, RUN against a real store.

A blob row and the bytes behind it are two different things in two different places: a row in
Postgres, and a file on a disk that may be another machine entirely. Nothing compared them. When
they disagreed the symptom appeared on somebody's phone — a download that fails on every sweep, for
ever, because the client is told the file exists and has no way to tell "gone" from "not yet" — and
finding out meant reading an access log and hand-querying the database.

The rules that make the scan safe to act on, each tested here:

  * `missing` means the store said NO (404/410). Anything else — a refusal, a timeout, a rate
    limiter, a redirect — is `unknown`, never missing. This drives a repair that deletes rows, and
    thousands of sequential probes make rate limiting the expected case, not the exotic one.
  * the repair drops ROWS ONLY. There is nothing in storage to delete; dropping the row is what lets
    a client stop being told the file is there.
  * the repair acts on the list the scan produced, never a fresh probe — a second look can answer
    differently in the seconds in between.
  * `deep` catches bytes that changed rather than vanished, and is off by default.
  * bytes with no row are reported and never deleted: a half-finished upload looks exactly the same.
"""
import asyncio
import os
import sys
import shutil
import tempfile
import unittest
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import blossom_service  # noqa: E402
from tests.blob_index_harness import alive, idx, load, put, shared_relay  # noqa: E402
_FIXTURES = (idx, shared_relay)   # pytest finds fixtures by module name

# The rows live in the REAL blob index — the shipped RelayServer over PosterChanDB (#161 moved them out of
# SQL). The first version of this file faked a SQL query whose filter it ignored, so a repair deleting an
# arbitrary row would have shipped green; the real store cannot agree with a wrong assumption.


class _Row:
    def __init__(self, sha, path, storage="local", size=10):
        self.sha256 = sha
        self.path = path
        self.storage = storage
        self.size = size
        self.created_at = 0


def _seed(rows):
    """Put `rows` into the index and load it, as the app's background loader does."""
    for r in rows:
        put(r.sha256, path=r.path, storage=r.storage, size=r.size)
    load()


def _run(coro):
    # `asyncio.run`, not `get_event_loop()`: under the full suite another test has usually closed the
    # main thread's loop by now, and these passed alone while failing in the run that matters.
    return asyncio.run(coro)


@pytest.mark.usefixtures("idx")
class LocalStoreScanTests(unittest.TestCase):
    """A local backend, where the answer is simply whether the file is on the disk."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pc-blossom-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.here = os.path.join(self.dir, "aa", "a" * 64)
        os.makedirs(os.path.dirname(self.here), exist_ok=True)
        with open(self.here, "wb") as fh:
            fh.write(b"hello")
        self.rows = [_Row("a" * 64, self.here), _Row("b" * 64, os.path.join(self.dir, "bb", "b" * 64))]
        self.cfg = {"backend": "local", "storage_url": "", "blob_dir": self.dir, "cache_mb": 0}
        _seed(self.rows)

    def _scan(self, **kw):
        with mock.patch.object(blossom_service, "_cfg", lambda db: self.cfg):
            return _run(blossom_service.scan_store(None, **kw))

    def test_a_row_whose_file_is_gone_is_reported_missing(self):
        out = self._scan()
        self.assertEqual(out["missing"], ["b" * 64], out)
        self.assertEqual(out["checked"], 2)
        self.assertEqual(out["unknown"], 0)

    def test_a_file_that_is_there_is_not_reported(self):
        out = self._scan()
        self.assertNotIn("a" * 64, out["missing"])

    def test_deep_catches_bytes_that_changed_rather_than_vanished(self):
        """The one fault a presence check cannot see: the file is there and is not what it claims."""
        shallow = self._scan()
        self.assertEqual(shallow["corrupt"], [], "a shallow scan hashed the files")
        deep = self._scan(deep=True)
        self.assertIn("a" * 64, deep["corrupt"],
                      "a file whose bytes do not match the sha it is stored under was not caught")

    def test_bytes_with_no_row_are_reported_and_never_deleted(self):
        stray = os.path.join(self.dir, "cc", "c" * 64)
        os.makedirs(os.path.dirname(stray), exist_ok=True)
        with open(stray, "wb") as fh:
            fh.write(b"orphan")
        out = self._scan()
        self.assertEqual(out["orphans"], 1, out)
        self.assertTrue(os.path.exists(stray), "the scan deleted a file it could not account for")

    def test_it_changes_nothing(self):
        before = sorted(os.listdir(self.dir))
        rows_before = alive()
        with mock.patch.object(blossom_service, "_cfg", lambda db_: self.cfg):
            _run(blossom_service.scan_store(None))
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        self.assertEqual(alive(), rows_before, "a read-only scan deleted rows")


@pytest.mark.usefixtures("idx")
class UnreadableLocalStoreTests(unittest.TestCase):
    """A disk that cannot be read is not a disk full of missing files.

    `os.path.isfile` answers False for a stale NFS handle, an unmounted volume, a permissions
    problem and a path with a typo in it. `blossom_storage_path` can point at an external mount —
    the admin panel has a whole section for it — so a scan run while that mount is down would
    otherwise report every row as missing and offer to drop the entire blob table, while every byte
    sat safely on the unmounted disk.
    """

    def test_a_path_that_cannot_be_read_is_unknown_not_gone(self):
        with mock.patch("os.stat", side_effect=PermissionError(13, "denied")):
            self.assertEqual(_run(blossom_service._probe_local("/somewhere/x")), "unknown")

    def test_a_path_that_is_genuinely_absent_is_gone(self):
        with mock.patch("os.stat", side_effect=FileNotFoundError(2, "nope")):
            self.assertEqual(_run(blossom_service._probe_local("/somewhere/x")), "gone")

    def test_a_missing_storage_directory_produces_no_verdict_at_all(self):
        """THE ONE THAT WOULD HAVE COST THE BLOB TABLE."""
        _seed([_Row(("%064x" % i), "/gone/%d" % i) for i in range(50)])
        cfg = {"backend": "local", "storage_url": "", "blob_dir": "/definitely/not/here",
               "cache_mb": 0}
        with mock.patch.object(blossom_service, "_cfg", lambda db: cfg):
            out = _run(blossom_service.scan_store(None))
        self.assertTrue(out["unreadable_store"], out)
        self.assertEqual(out["missing"], [], "an unreadable store reported every row as lost")
        self.assertEqual(out["unknown"], 50, out)


@pytest.mark.usefixtures("idx")
class ProxyStoreScanTests(unittest.TestCase):
    """A storage server, where "no answer" and "not there" are different things and must stay so."""

    def setUp(self):
        self.rows = [_Row("a" * 64, "blossom/aa/" + "a" * 64, storage="proxy"),
                     _Row("b" * 64, "blossom/bb/" + "b" * 64, storage="proxy")]
        self.cfg = {"backend": "proxy", "storage_url": "http://store.example",
                    "blob_dir": "", "cache_mb": 0}
        _seed(self.rows)

    def _scan(self, status_by_sha, **kw):
        class _Resp:
            def __init__(self, code):
                self.status_code = code
                self.content = b""

        class _Client:
            async def head(self, url, headers=None):
                for sha, code in status_by_sha.items():
                    if sha in url:
                        return _Resp(code)
                return _Resp(500)

            async def get(self, url, headers=None):
                return await self.head(url, headers=headers)

        with mock.patch.object(blossom_service, "_cfg", lambda db: self.cfg), \
                mock.patch.object(blossom_service, "_client", lambda: _Client()), \
                mock.patch.object(blossom_service, "_proxy_headers", lambda: {}):
            return _run(blossom_service.scan_store(None, **kw))

    def test_a_404_is_missing(self):
        out = self._scan({"a" * 64: 200, "b" * 64: 404})
        self.assertEqual(out["missing"], ["b" * 64], out)
        self.assertEqual(out["unknown"], 0, out)

    def test_a_rate_limiter_is_not_missing(self):
        """Thousands of sequential probes make 429 the expected answer, not the exotic one — and this
        list drives a repair that deletes rows."""
        out = self._scan({"a" * 64: 429, "b" * 64: 429})
        self.assertEqual(out["missing"], [], "a rate-limited probe was reported as data loss")
        self.assertEqual(out["unknown"], 2, out)

    def test_a_refusal_or_a_server_error_is_not_missing(self):
        for code in (403, 500, 502, 503):
            out = self._scan({"a" * 64: code, "b" * 64: code})
            self.assertEqual(out["missing"], [], "HTTP %d was read as missing" % code)

    def test_a_dead_store_is_not_missing(self):
        class _Boom:
            async def head(self, *_a, **_k):
                raise RuntimeError("connection refused")

            async def get(self, *_a, **_k):
                raise RuntimeError("connection refused")

        with mock.patch.object(blossom_service, "_cfg", lambda db: self.cfg), \
                mock.patch.object(blossom_service, "_client", lambda: _Boom()), \
                mock.patch.object(blossom_service, "_proxy_headers", lambda: {}):
            out = _run(blossom_service.scan_store(None))
        self.assertEqual(out["missing"], [], "an unreachable store reported every file as lost")
        self.assertEqual(out["unknown"], 2)

    def test_orphans_are_not_guessed_for_a_storage_server(self):
        """It has no listing this can walk, and guessing would be worse than saying nothing."""
        out = self._scan({"a" * 64: 200, "b" * 64: 200})
        self.assertEqual(out["orphans"], 0)


@pytest.mark.usefixtures("idx")
class ForgetMissingTests(unittest.TestCase):
    """The repair asks the store AGAIN about every row before dropping it.

    A second look answering "gone" tells us nothing new; one answering "there" is proof we must not
    delete. It costs one request per row being dropped, which is the cheapest possible insurance
    against a scan that ran while a mount was down, a prefix was being moved, or a re-upload was in
    flight — and the row is the only record of the sha → path → owner mapping, so dropping it takes
    the file out of the drive listing, Notes' attachments and the music library.
    """

    def setUp(self):
        self.rows = [_Row("a" * 64, "/x"), _Row("b" * 64, "/y")]
        _seed(self.rows)
        self.patches = [
            mock.patch.object(blossom_service, "drop_meta", lambda s: None),
            mock.patch.object(blossom_service, "_cache_drop", lambda s: None),
            mock.patch.object(blossom_service, "_cfg",
                              lambda db: {"backend": "local", "storage_url": "", "blob_dir": "/x",
                                          "cache_mb": 0}),
        ]
        for p_ in self.patches:
            p_.start()
            self.addCleanup(p_.stop)

    def _forget(self, shas, states):
        async def probe(path):
            return states.get(path, "gone")
        with mock.patch.object(blossom_service, "_probe_local", probe):
            return _run(blossom_service.forget_missing(None, shas))

    def test_it_drops_the_row_it_was_given_and_no_other(self):
        out = self._forget(["a" * 64], {})
        self.assertEqual(out["removed"], 1, out)
        self.assertEqual(alive(), {"b" * 64}, "it deleted a row other than the one it was asked about")

    def test_a_row_whose_bytes_turn_out_to_be_there_is_kept(self):
        """The whole reason for the second look."""
        out = self._forget(["a" * 64], {"/x": "there"})
        self.assertEqual(out["removed"], 0, out)
        self.assertEqual(out["kept"], 1, out)
        self.assertEqual(alive(), {"a" * 64, "b" * 64}, "it dropped a row for a file that is present")

    def test_a_row_the_store_cannot_be_asked_about_is_kept(self):
        out = self._forget(["a" * 64], {"/x": "unknown"})
        self.assertEqual(out["removed"], 0, out)
        self.assertEqual(out["unknown"], 1, out)
        self.assertEqual(alive(), {"a" * 64, "b" * 64})

    def test_it_refuses_to_drop_most_of_the_store(self):
        """A store that looks mostly missing is a store that cannot be reached — the rule folder
        sync and the phone book both use, for the same reason."""
        rows = [_Row(("%064x" % i), "/x") for i in range(40)]
        _seed(rows)
        before = alive()
        out = self._forget([r.sha256 for r in rows], {})
        self.assertEqual(out["removed"], 0, out)
        self.assertIn("refusing", out["refused"])
        self.assertEqual(alive(), before)

    def test_a_small_delete_is_never_questioned(self):
        rows = [_Row(("%064x" % i), "/x") for i in range(40)]
        _seed(rows)
        out = self._forget([rows[0].sha256, rows[1].sha256], {})
        self.assertEqual(out["removed"], 2, out)

    def test_it_ignores_anything_that_is_not_a_sha(self):
        before = alive()
        out = self._forget(["nope", "", None], {})
        self.assertEqual(out["removed"], 0)
        self.assertEqual(alive(), before, "it changed the index although nothing was asked")

    def test_it_never_touches_storage(self):
        """"Missing" means the bytes are already gone; there is nothing to delete, and a repair that
        reached for storage could delete something that was merely unreachable."""
        import inspect
        src = inspect.getsource(blossom_service.forget_missing)
        for danger in ("os.remove", "unlink", "rmtree", "_proxy_delete", "delete-file"):
            self.assertNotIn(danger, src, "the repair touches storage: %s" % danger)


@pytest.mark.usefixtures("idx")
class RowsThatCannotBeAskedTests(unittest.TestCase):
    """A ROW knows where its own bytes went. The CONFIG only knows where the next upload would go.

    Measured on this deployment: 76,775 rows saying `storage=proxy` under a node whose backend is
    now `local`, so there is no storage server to ask and every one of them scores `unknown`. The
    scan was right to refuse a verdict and said nothing at all about why — a screen reading "76,775
    could not be checked" beside "Everything the database claims is there" is two sentences that
    cannot both be acted on.
    """

    def setUp(self):
        self.rows = [_Row("a" * 64, "blossom/aa/" + "a" * 64, storage="proxy"),
                     _Row("b" * 64, "blossom/bb/" + "b" * 64, storage="proxy")]
        # The shape that produced it: proxy rows, no proxy. The blob directory is real and readable
        # — this is not the unmounted-disk case, it is a node that moved backends.
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = {"backend": "local", "storage_url": "", "blob_dir": self.dir, "cache_mb": 0}
        _seed(self.rows)

    def _scan(self):
        with mock.patch.object(blossom_service, "_cfg", lambda db: self.cfg):
            return _run(blossom_service.scan_store(None))

    def test_this_is_not_the_unreadable_store_case(self):
        """Which is the guard that would otherwise make this test pass for the wrong reason: an
        absent blob directory short-circuits the whole scan before a single row is looked at."""
        self.assertFalse(self._scan()["unreadable_store"])

    def test_they_are_never_reported_as_missing(self):
        out = self._scan()
        self.assertEqual(out["missing"], [], "rows nothing could be asked about were called lost")
        self.assertEqual(out["unknown"], 2)

    def test_the_scan_says_why_it_could_not_ask(self):
        out = self._scan()
        self.assertIn("storage proxy", out["cannot"])
        self.assertIn("2 row", out["cannot"])

    def test_a_normal_scan_carries_no_such_warning(self):
        _run(blossom_service.blob_index.adelete("b" * 64))      # the one row this case wants
        load()
        self.cfg = dict(self.cfg, backend="proxy", storage_url="http://store.example")

        class _Resp:
            status_code = 200
            content = b""

        class _Client:
            async def head(self, *_a, **_k):
                return _Resp()

            async def get(self, *_a, **_k):
                return _Resp()

        with mock.patch.object(blossom_service, "_cfg", lambda db: self.cfg), \
                mock.patch.object(blossom_service, "_client", lambda: _Client()), \
                mock.patch.object(blossom_service, "_proxy_headers", lambda: {}):
            out = _run(blossom_service.scan_store(None))
        self.assertEqual(out["cannot"], "")


class TheButtonTests(unittest.TestCase):
    """It is reachable, it asks before it deletes, and it acts on the scan it just ran."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "templates", "admin", "tabs", "blossom.html"),
                  encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(root, "static", "js", "admin.js"), encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_the_tab_has_a_scan_control(self):
        self.assertIn('id="bl_scan_btn"', self.html)
        self.assertIn('id="bl_scan_deep"', self.html)
        self.assertIn('id="bl_scan_out"', self.html)

    def test_deep_is_opt_in(self):
        self.assertIn('type="checkbox" id="bl_scan_deep"', self.html)
        self.assertNotIn('id="bl_scan_deep" checked', self.html)

    def test_the_button_is_wired(self):
        self.assertIn("bl_scan_btn", self.js)
        self.assertIn("/api/admin/blossom/scan", self.js)

    def test_the_repair_asks_first_and_says_nothing_is_deleted_from_storage(self):
        """`pcConfirm`, not the native `confirm` — the admin panel's dialogs were converted because
        a native one opens a REAL window in the desktop shell (it split the screen in half from the
        relay tab's prune button) and is suppressed outright in the APK's WebView, where the branch
        behind it then never runs at all.

        The `await` half is the one that matters here. `pcConfirm` returns a PROMISE: unawaited it
        is a truthy object, so this repair would proceed unconditionally — and what it proceeds to
        do is tombstone index entries on every device for ninety days. That is strictly worse than
        the native dialog it replaced, so it is asserted rather than assumed."""
        seg = self.js[self.js.index("async function forget()"):]
        self.assertIn("await pcConfirm(", seg,
                      "the repair must ask through the panel's own dialog, and must await it")
        self.assertNotIn("window.confirm(", seg)
        self.assertIn("Nothing is deleted from storage", seg)

    def test_the_repair_sends_the_list_the_scan_produced(self):
        seg = self.js[self.js.index("async function forget()"):]
        self.assertIn("shas: lastScan.missing", seg,
                      "it re-probes instead of using the answers the admin was shown")

    def test_unknown_answers_are_shown_as_unknown_not_as_loss(self):
        seg = self.js[self.js.index("async function scan()"):]
        self.assertIn("Not counted as missing", seg)

    def test_the_reason_nothing_could_be_asked_reaches_the_screen(self):
        seg = self.js[self.js.index("async function scan()"):]
        self.assertIn("s.cannot", seg, "the scan explains why it could not ask and nothing shows it")

    def test_it_does_not_call_a_store_it_could_not_read_healthy(self):
        """"Everything the database claims is there" next to "76,775 could not be checked" is a
        contradiction, and the reassuring half is the one people act on."""
        seg = self.js[self.js.index("async function scan()"):]
        self.assertIn("s.checked > s.unknown", seg)


if __name__ == "__main__":
    unittest.main()
