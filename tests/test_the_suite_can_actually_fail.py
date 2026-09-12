"""Every guard below is broken ON PURPOSE, and the test that owns it must NOTICE.

Run: venv-unified/bin/python -m pytest tests/test_the_suite_can_actually_fail.py

WHY THIS EXISTS. A passing suite is evidence of nothing unless the tests can fail. This repo has
shipped, repeatedly, with green tests over broken code, and every single time the mechanism was the
same — the assertion was not measuring what it appeared to measure:

  * a `str.replace()` proof whose needle had different whitespace, so the "mutation" was a no-op and
    the suite dutifully reported "0 failures" about unmodified code;
  * `grep -c "^FAILED"` against output pytest does not phrase that way — always 0, always green;
  * a stylesheet check that searched for a selector as a SUBSTRING and so matched a rule gated to one
    platform, while the feature was dark everywhere else;
  * a DOM harness that memoised its nodes for ever, so a handler bound to a node the real app had
    already replaced kept working in the test — that one shipped the Concord attach bug AND then the
    Concord paste bug, green both times;
  * a revision-bump guard that asked "did the ebuild change?" when the question is "did its VERSION
    change?" — an unrelated edit inside the file satisfied it.

None of those are careless typing. They are all the same shape: a check that cannot distinguish the
working world from the broken one. The only way to find that shape is to BUILD the broken world and
see whether anything objects.

So each entry names a file, a mutation that reintroduces a real bug this repo has actually shipped,
and the test that must go red. The mutation is applied, the test is run, the file is restored in a
`finally`. If a mutation ever stops applying, that is a failure too — a proof that silently matches
nothing is the very thing this file exists to prevent.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "venv-unified/bin/python")

# THE MUTATION HAPPENS IN A THROWAWAY WORKTREE, NEVER IN THE WORKING TREE.
#
# checkall.py runs the pytest suites and ~20 browser checks CONCURRENTLY, and those browsers load
# `static/js/client/app.js` from disk. Editing a shipped file here — even for the half-second a
# mutation lives — would corrupt whatever else happened to read it, and the resulting failure would
# land on an innocent check with nothing to explain it. That is the same rule docs/TESTING.md states
# for `streamserver/mediamtx.pid`: a check must never write into the working tree's live state.
#
# A worktree is the cheap way to get an honest copy: 3,113 tracked files, no history, no venv. The
# uncommitted work is carried across as a patch, so this measures the tree as it is RIGHT NOW and
# not as it was at the last commit — which for a guard being added in this very session is the
# whole point.

# (label, source file, needle, replacement, test target)
#
# Keep this list SHORT and load-bearing. Each entry costs one pytest subprocess, and a slow suite is
# a suite people skip — which is its own way of having no tests.
MUTATIONS = [
    (
        "the focused window frame stops using the client's palette",
        "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini",
        "active_color = \\#3ce8ffff",
        "active_color = \\#ff00ffff",
        "tests/test_window_frame_matches_the_client.py",
    ),
    (
        "a Blossom listing ignores the caller's limit and returns the whole 9.7 MB",
        "app/routers/blossom.py",
        "if limit is not None and limit >= 0:",
        "if False:",
        "tests/test_blossom_list_can_be_bounded.py",
    ),
    (
        "the Meme Builder binds paste to the node it was handed, which a render replaces",
        "static/js/client/meme.js",
        "const host=_mbPasteRoot;",
        "const host=root;",
        "tests/client/test_pasting_reaches_the_builder_and_the_email.py",
    ),
    (
        "the mail composer stops reading clipboard ITEMS, so a pasted screenshot is lost",
        "static/js/client/app.js",
        "for(const it of [...(cd.items||[])]){",
        "for(const it of []){",
        "tests/client/test_pasting_reaches_the_builder_and_the_email.py",
    ),
]


def _git(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd or ROOT), capture_output=True, text=True)


def _make_worktree(into: Path) -> str:
    """A detached checkout of HEAD with this session's uncommitted changes applied. '' on success."""
    made = _git("worktree", "add", "--detach", str(into), "HEAD")
    if made.returncode != 0:
        return f"could not create a worktree: {made.stderr.strip()[:200]}"
    diff = _git("diff", "HEAD")            # staged + unstaged, tracked files and staged new ones
    if diff.returncode != 0:
        return f"could not read the working diff: {diff.stderr.strip()[:200]}"
    if diff.stdout.strip():
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                                 cwd=str(into), input=diff.stdout, capture_output=True, text=True)
        if applied.returncode != 0:
            return f"could not apply the working diff: {applied.stderr.strip()[:300]}"
    return ""


def _run(target: str, cwd: Path) -> int:
    return subprocess.run(
        [PY, "-B", "-m", "pytest", target, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=str(cwd), capture_output=True, text=True,
    ).returncode


class TheSuiteCanFail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="pc-mutate-"))
        cls.tree = cls._tmp / "wt"
        cls.why = _make_worktree(cls.tree)

    @classmethod
    def tearDownClass(cls):
        _git("worktree", "remove", "--force", str(cls.tree))
        shutil.rmtree(cls._tmp, ignore_errors=True)
        _git("worktree", "prune")

    def setUp(self):
        # A skip that says WHY. Never a silent pass — "could not run" is not "passed".
        if self.why:
            self.skipTest(self.why)

    def test_every_guard_is_noticed_when_it_is_broken(self):
        for label, rel, needle, replacement, target in MUTATIONS:
            with self.subTest(guard=label):
                path = self.tree / rel
                self.assertTrue(path.exists(), f"{rel} is missing from the worktree")
                original = path.read_text()

                # A mutation that does not apply proves nothing and must be reported as loudly as a
                # test that does not fail — this is the exact shape that produced a false "0 failures".
                self.assertIn(needle, original,
                              f"the mutation for {rel} no longer applies; this proof is vacuous")
                self.assertEqual(original.count(needle), 1,
                                 f"{needle!r} is ambiguous in {rel}; the mutation would be imprecise")

                try:
                    path.write_text(original.replace(needle, replacement, 1))
                    self.assertNotEqual(path.read_text(), original, "the mutation did not land")
                    # Exit CODE, never scraped output: reading pytest's prose for the word FAILED is
                    # how a green run was reported over a suite that never matched anything.
                    self.assertNotEqual(
                        _run(target, self.tree), 0,
                        f"{target} still PASSED with this broken: {label}.\n"
                        f"That test does not measure what it claims to.")
                finally:
                    path.write_text(original)

    def test_the_working_tree_was_never_touched(self):
        """The mutations happen in a copy. If one ever escapes, every later check is poisoned."""
        for _, rel, needle, _, _ in MUTATIONS:
            live = ROOT / rel
            self.assertIn(needle, live.read_text(),
                          f"{rel} in the WORKING TREE was mutated — the isolation failed")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
