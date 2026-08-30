"""The check suite must find its venv from inside a git worktree.

Run: venv-unified/bin/python -m pytest tests/test_check_suite_runs_in_a_worktree.py

`venv-unified/` is not a tracked file, so a `git worktree` has none: `.claude/worktrees/<name>/`
holds the source and nothing else. Both entry points resolved the interpreter as
"`venv-unified/bin/python` under this directory, else fall back", and the fallback is a bare system
python with no `websockets` — which is what every check that drives a browser or a relay imports.

Measured on server1 before this was fixed: `./test.sh` in a worktree printed a full board in EIGHT
SECONDS, with 13 checks red on `ModuleNotFoundError: No module named 'websockets'` and 32 more
SKIPPED saying "websockets not installed". 45 of 85 did not run. Nothing on screen named the
interpreter, and every message pointed at a missing dependency rather than at the wrong python —
so the obvious next move is to install a package that is already installed, two directories up.

This matters more than a normal environment bug because a worktree is the WORKING PATTERN here:
`EnterWorktree` is what an agent is told to use before editing, and CLAUDE.md's deploy rule is that
the suite runs before and after. "I ran the tests" in a worktree meant a board that checked almost
nothing and still printed a verdict.

Built against a REAL git worktree in a temp directory rather than against the live ROOT: from the
main checkout the old code resolves correctly and a test of ROOT would pass while the bug it exists
to catch was fully present everywhere it actually fires.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

HAVE_GIT = shutil.which("git") is not None


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, timeout=60)


@unittest.skipUnless(HAVE_GIT, "git is not installed here")
class TheSuiteFindsItsVenvFromAWorktree(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pcai-wt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.main = self.tmp / "repo"
        self.main.mkdir()
        for a in (["init", "-q", "-b", "master"], ["config", "user.email", "t@t"],
                  ["config", "user.name", "t"]):
            _git(*a, cwd=self.main)
        (self.main / "f").write_text("x")
        _git("add", "-A", cwd=self.main)
        _git("commit", "-qm", "c", cwd=self.main)
        # The venv lives in the MAIN checkout and is untracked — exactly as it is on a real node.
        vb = self.main / "venv-unified" / "bin"
        vb.mkdir(parents=True)
        self.venv = vb / "python"
        self.venv.write_text("#!/bin/sh\nexec true\n")
        self.venv.chmod(0o755)
        self.wt = self.tmp / "wt"
        r = _git("worktree", "add", "-q", "-b", "wtbranch", str(self.wt), cwd=self.main)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_worktree_has_no_venv_of_its_own(self):
        """The premise. If this ever stops being true the rest of the file is about nothing."""
        self.assertTrue(self.wt.is_dir())
        self.assertFalse((self.wt / "venv-unified").exists(),
                         "the fixture worktree has a venv — it is no longer reproducing the case")

    def test_checkall_finds_the_main_checkouts_venv_from_the_worktree(self):
        import checkall
        got = checkall._interpreter(self.wt)
        self.assertEqual(str(self.venv), got,
                         "from a worktree the runner picked %r instead of the main checkout's "
                         "venv at %r — every browser check then fails or skips on a missing "
                         "module that is installed in the venv it did not use" % (got, self.venv))

    def test_it_still_prefers_a_venv_in_the_directory_it_was_given(self):
        """A normal checkout must not start reaching elsewhere."""
        import checkall
        self.assertEqual(str(self.venv), checkall._interpreter(self.main))

    def test_it_falls_back_rather_than_crashing_outside_a_repo(self):
        """Docker and a plain `pip install -r requirements.txt` run the checks with the active
        interpreter and no venv directory at all. That is legitimate, so the resolution must not
        raise — it is `_interpreter_is_equipped()` that stops the fallback being silent."""
        import checkall
        plain = self.tmp / "notarepo"
        plain.mkdir()
        self.assertEqual(sys.executable, checkall._interpreter(plain))

    def test_test_sh_resolves_the_same_way(self):
        """The two entry points must agree: `./test.sh` execs checkall with an interpreter it chose
        ITSELF, so fixing only the Python half leaves the shell half picking a bare python3.

        This RUNS the resolution lifted out of the shipped test.sh, in the fixture worktree. Two
        weaker versions were written first and both were verified not to catch a reverted test.sh:
        an `assertIn("--git-common-dir", sh)` passes on the COMMENT that explains the fix, and a
        hardcoded copy of the snippet only ever tests itself.
        """
        sh = (ROOT / "test.sh").read_text().splitlines()
        start = next((i for i, ln in enumerate(sh) if ln.startswith("PY=")), None)
        self.assertIsNotNone(start, "test.sh no longer assigns PY")
        end = next((i for i, ln in enumerate(sh[start:], start)
                    if ln.startswith("exec ")), len(sh))
        block = "\n".join(sh[start:end]) + '\necho "$PY"\n'
        r = subprocess.run(["bash", "-c", block], cwd=str(self.wt),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(
            str(self.venv), r.stdout.strip(),
            "run from a git worktree, test.sh's own interpreter resolution picked %r instead of "
            "the main checkout's venv at %r — so ./test.sh execs checkall.py under a python with "
            "no websockets and the browser half of the suite skips.\nThe block it ran was:\n%s"
            % (r.stdout.strip(), self.venv, block))


class TheRunnerSaysWhenItCannotRunAnything(unittest.TestCase):
    """A wrong interpreter must never again look like a clean run."""

    def test_the_equipped_probe_exists_and_names_the_interpreter(self):
        import checkall
        self.assertTrue(hasattr(checkall, "_interpreter_is_equipped"))
        src = (ROOT / "scripts" / "checkall.py").read_text()
        self.assertIn("CANNOT RUN THEM", src,
                      "the board no longer prints the interpreter banner, so a run that skipped "
                      "half of itself for one fixable reason prints no reason at all")
        self.assertIn("interpreter:", src)

    def test_the_probe_asks_for_websockets_and_not_for_playwright(self):
        """The browser is driven over CDP. Demanding the playwright PYTHON package made the probe
        call a working interpreter broken — one that had just run a check to completion."""
        import checkall
        import inspect
        body = inspect.getsource(checkall._interpreter_is_equipped)
        self.assertIn("import websockets", body)
        self.assertNotIn('"import websockets, playwright"', body)

    def test_this_checkout_is_actually_equipped(self):
        """And the live one: whatever runs this test suite must be able to run a browser check.
        Green here and red in `./test.sh` would mean the two are using different pythons."""
        import checkall
        ok, missing = checkall._interpreter_is_equipped()
        self.assertTrue(ok, "the resolved interpreter %s cannot import %r — the browser checks "
                            "will skip" % (checkall.PY, missing))


if __name__ == "__main__":
    unittest.main()
