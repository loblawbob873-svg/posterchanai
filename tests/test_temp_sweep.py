"""The startup temp janitor, and the coverage check that keeps its allowlist honest.

Run: venv-unified/bin/python -m unittest tests.test_temp_sweep

The sweep exists because `finally: rmtree` cannot survive SIGKILL, and on this deployment /tmp is
a tmpfs — so a render orphaned by an OOM kill is pinned RAM, not just a stale file.

The coverage test is the important one. An allowlist of 55 prefixes is only correct for as long as
it matches the code, and the way this breaks is silent: someone adds a temp path, it is never
swept, and nothing fails. That is exactly how 15 temp creations ended up with NO prefix at all —
landing as bare `tmpXXXX`, indistinguishable from every other program's temp files and therefore
unsweepable by anything that isn't willing to delete files it cannot attribute.
"""
import ast
import os
import time
import unittest

from app.services import temp_sweep_service as sweep

_ROOTS = ("app", "botframework")
_TEMP_FUNCS = {"mkdtemp", "NamedTemporaryFile", "mkstemp", "TemporaryDirectory", "TemporaryFile"}
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _temp_calls():
    """Every tempfile.* creation in the app, as (path, lineno, prefix-or-None), skipping the ones
    that pass an explicit `dir=` — those land next to their own state (the bots' atomic-write
    `.hmids_`/`.c4ids_` files) and never reach the shared temp dir."""
    out = []
    for root in _ROOTS:
        for dirpath, _dirnames, filenames in os.walk(os.path.join(_REPO, root)):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except (OSError, SyntaxError):                      # pragma: no cover
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = (node.func.attr if isinstance(node.func, ast.Attribute)
                            else getattr(node.func, "id", None))
                    if name not in _TEMP_FUNCS:
                        continue
                    kwargs = {k.arg: k for k in node.keywords}
                    if "dir" in kwargs:
                        continue
                    prefix = None
                    kw = kwargs.get("prefix")
                    if kw is not None and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value
                    rel = os.path.relpath(path, _REPO)
                    out.append((rel, node.lineno, prefix))
    return out


class TestTheAllowlistMatchesTheCode(unittest.TestCase):
    def test_every_temp_prefix_is_swept(self):
        """A prefix the sweep does not know about is a temp path that leaks forever."""
        missing = sorted({p for _f, _l, p in _temp_calls()
                          if p and not p.startswith(sweep._APP_TEMP_PREFIXES)})
        self.assertEqual(missing, [],
                         "these temp prefixes are not in temp_sweep_service._APP_TEMP_PREFIXES, "
                         "so nothing will ever clean them up: " + ", ".join(missing))

    def test_no_temp_file_is_created_without_a_prefix(self):
        """An unprefixed temp lands as a bare `tmpXXXX`, which is indistinguishable from every
        other program's temp files — so it can never be swept without deleting things that are not
        ours. Add `prefix=` (and list it in _APP_TEMP_PREFIXES), or pass `dir=` if it belongs
        somewhere else entirely."""
        bare = [f"{f}:{l}" for f, l, p in _temp_calls() if p is None]
        self.assertEqual(bare, [],
                         "unprefixed tempfile creation(s) — these can never be swept: "
                         + ", ".join(bare))

    def test_the_allowlist_has_no_dead_entries(self):
        """A prefix nobody creates any more is dead weight that makes the list harder to trust."""
        live = {p for _f, _l, p in _temp_calls() if p}
        dead = sorted(set(sweep._APP_TEMP_PREFIXES) - live)
        self.assertEqual(dead, [],
                         "these are in the allowlist but nothing creates them: " + ", ".join(dead))


class TestTheSweep(unittest.TestCase):
    def setUp(self):
        import shutil as _sh
        import tempfile as _tf
        self.tmp = _tf.mkdtemp(prefix="sweeptest_")
        self.addCleanup(_sh.rmtree, self.tmp, True)

    def _sweep(self, **kw):
        """Always via the tmpdir ARGUMENT. Patching tempfile.gettempdir would swap the stdlib
        function out for the whole process — the suite shares one — and silently redirect every
        other temp path created while these tests run."""
        return sweep.sweep_temp_orphans(tmpdir=self.tmp, **kw)

    def _make(self, name, age_s, nested=False):
        path = os.path.join(self.tmp, name)
        if nested:
            os.makedirs(os.path.join(path, "deep"))
            target = os.path.join(path, "deep", "f.png")
        else:
            target = path
        with open(target, "wb") as f:
            f.write(b"x" * 32)
        when = time.time() - age_s
        for p in ({path, target} if nested else {path}):
            os.utime(p, (when, when))
        if nested:                                   # the dirs too, or the walk sees "now"
            os.utime(os.path.join(path, "deep"), (when, when))
        return path

    def test_it_removes_an_old_orphan(self):
        p = self._make("media_frames_dead", age_s=48 * 3600, nested=True)
        self.assertEqual(self._sweep(), 1)
        self.assertFalse(os.path.exists(p))

    def test_it_leaves_a_recent_one_alone(self):
        """A render in flight must survive — the age gate is the only thing standing between this
        janitor and a live job on a box where something else shares the prefix."""
        p = self._make("media_frames_live", age_s=60, nested=True)
        self.assertEqual(self._sweep(), 0)
        self.assertTrue(os.path.exists(p))

    def test_a_stale_looking_dir_with_fresh_contents_survives(self):
        """A directory's own mtime does not move when a file is written DEEP inside it, so age has
        to be measured over the whole tree. Getting this wrong deletes a running render."""
        p = os.path.join(self.tmp, "media_frames_writing")
        os.makedirs(os.path.join(p, "deep"))
        with open(os.path.join(p, "deep", "f.png"), "wb") as f:
            f.write(b"x")
        old = time.time() - 48 * 3600
        os.utime(p, (old, old))                       # top looks ancient, contents are new
        self.assertEqual(self._sweep(), 0)
        self.assertTrue(os.path.exists(p))

    def test_it_never_touches_what_is_not_ours(self):
        """THE safety property. The temp dir is shared with every other program on the box."""
        keep = [self._make(n, age_s=48 * 3600)
                for n in ("tmpABCD1234", "systemd-private-xyz", "pcshot_notours", "snap.foo")]
        self._sweep()
        for p in keep:
            self.assertTrue(os.path.exists(p), f"the sweep deleted {p}, which is not ours")

    def test_a_symlink_is_unlinked_not_followed(self):
        """Removing the link must not remove its target."""
        target = os.path.join(self.tmp, "target.bin")
        with open(target, "wb") as f:
            f.write(b"precious")
        link = os.path.join(self.tmp, "media_frames_link")
        os.symlink(target, link)
        old = time.time() - 48 * 3600
        os.utime(link, (old, old), follow_symlinks=False)
        self._sweep()
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(target), "the sweep followed a symlink and ate its target")

    def test_it_never_raises(self):
        """A janitor that can break startup is not worth having."""
        self.assertEqual(sweep.sweep_temp_orphans(tmpdir="/nonexistent/temp/dir"), 0)


if __name__ == "__main__":                                          # pragma: no cover
    unittest.main()
