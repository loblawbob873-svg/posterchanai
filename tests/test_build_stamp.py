"""The build stamp must track the working tree, including after a deploy that restarts nothing.

Five days of a folder-sync investigation went into testing devices running code from before the fix,
because nothing in the app said which commit it was. The stamp exists to end that — so a stamp that
is WRONG is worse than none, and there is one specific way for it to go wrong here: `sync.sh` pulls
every node but restarts only what `scripts/deploy_targets.py` says needs restarting, because a
restart is an outage. A UI-only deploy therefore serves new JavaScript inside a Python process that
started on the previous commit. Measured exactly once, immediately after shipping the first version:
the tree was on ac7b1252 and the page still said 729898d8.
"""
import os
import subprocess
import sys
import unittest
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class BuildStamp(unittest.TestCase):
    def _head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    def test_it_reports_the_commit_the_tree_is_on(self):
        from app.routers.client import _build_sha
        head = self._head()
        if not head:
            self.skipTest("not a git checkout")
        self.assertTrue(head.startswith(_build_sha()),
                        "the stamp does not match HEAD — it names a commit this is not")

    def test_it_is_not_frozen_for_the_life_of_the_process(self):
        """The whole failure mode: resolved once and held, so a pull without a restart keeps
        answering the old commit for as long as the worker lives."""
        import app.routers.client as C
        head = self._head()
        if not head:
            self.skipTest("not a git checkout")
        C._build_sha()                                   # prime whatever cache it keeps
        C._BUILD_SHA = ("deadbeef", 0.0)                 # a stale entry from before a pull
        self.assertTrue(head.startswith(C._build_sha()),
                        "a stale cached sha survived — a UI-only deploy would keep reporting the "
                        "commit the process started on")

    def test_it_never_shells_out_on_the_hot_path(self):
        """It is read per page render; a subprocess there is a fork per request."""
        src = open(os.path.join(ROOT, "app", "routers", "client.py"), encoding="utf-8").read()
        i = src.index("def _build_sha()")
        seg = src[i:i + 3600]
        self.assertIn('os.path.join(gitdir, "HEAD")', seg,
                      "it must read the ref files, not shell out every time")

    def test_it_resolves_a_linked_worktrees_git_pointer(self):
        """CI/review runs from linked worktrees where `.git` is a file pointing at the real dir."""
        import app.routers.client as C
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "tree")
            module = os.path.join(root, "app", "routers", "client.py")
            gitdir = os.path.join(td, "repo.git", "worktrees", "review")
            os.makedirs(os.path.dirname(module)); os.makedirs(gitdir)
            with open(os.path.join(root, ".git"), "w", encoding="utf-8") as fh:
                fh.write("gitdir: " + gitdir + "\n")
            want = "1234567890abcdef" * 4
            with open(os.path.join(gitdir, "HEAD"), "w", encoding="utf-8") as fh:
                fh.write(want + "\n")
            old_file, old_cache = C.__file__, C._BUILD_SHA
            try:
                C.__file__ = module; C._BUILD_SHA = ("", 0.0)
                self.assertEqual(C._build_sha(), want[:8])
            finally:
                C.__file__, C._BUILD_SHA = old_file, old_cache

    def test_the_page_carries_it(self):
        tpl = open(os.path.join(ROOT, "templates", "client.html"), encoding="utf-8").read()
        self.assertIn('window.__PC_BUILD="{{ build }}"', tpl)

    def test_both_bundles_stamp_their_own(self):
        """A bundle fetches index.html from the live server, which may be on a different commit than
        the checkout being built — so the server's answer is exactly the wrong one to keep."""
        for f in ("mobile/build-www.sh", "desktop/build-www.sh"):
            src = open(os.path.join(ROOT, f), encoding="utf-8").read()
            self.assertIn("__PC_BUILD", src, "%s does not stamp the bundle" % f)
            self.assertIn("rev-parse", src, "%s does not read its own commit" % f)


if __name__ == "__main__":
    unittest.main()
