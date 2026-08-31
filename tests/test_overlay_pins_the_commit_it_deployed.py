"""The overlay shipped the PREVIOUS desktop build, so an installed machine never got the deploy.

"Why does desktop not work right then!"

`bump_desktop_overlay.py` pinned the NEWEST existing `desktop-v<version>` release, and `sync.sh`
runs it BEFORE the push — so the newest release is always the previous commit's build. Measured on
the real machine: `emerge` installed a package correctly named 1.0.1326 whose bundled client was
63ccd0de, one deploy behind, while CI had already published 1.0.1327 from the commit being deployed.
Every fix in that deploy was absent from the desktop — `_fxOpenFolder`, `askEditorToSave`,
`driftPlan`, `moveToOtherMonitor` all missing from the asar — with a version number that looked
perfectly fresh and nothing anywhere saying otherwise. The script's own comment said being a version
or two behind was "FINE"; it is not, when the point of the deploy is to ship a fix.

It cannot be fixed by reordering — the desktop build takes ~15 minutes and a deploy must not block
on it. So the version is resolvable BY COMMIT, "not published yet" is its own answer rather than a
silent fallback to something older, and the deploy says out loud that PosterChanOS is not on this
commit yet and prints the one command that finishes the job.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUMP = (ROOT / "scripts" / "bump_desktop_overlay.py").read_text(encoding="utf-8")
SYNC = (ROOT / "sync.sh").read_text(encoding="utf-8")


class AVersionIsResolvedByTheCommitItWasBuiltFrom(unittest.TestCase):
    def test_there_is_a_lookup_by_commit_at_all(self):
        self.assertIn("def _tag_for_commit(sha)", BUMP)
        self.assertIn("def _releases()", BUMP)

    def test_the_build_commit_comes_from_the_rest_payload(self):
        """`gh release list --json targetCommitish` is not a valid field — it only exists on the
        REST payload and on `release view`. Asking for it the wrong way exits non-zero, which in a
        deploy script reads as "could not list releases"."""
        body = BUMP[BUMP.index("def _releases()"):BUMP.index("def _tag_for_commit")]
        self.assertIn("gh", body)
        self.assertIn("api", body)
        self.assertIn("target_commitish", body)
        self.assertNotIn('"--json", "tagName,targetCommitish"', body)

    def test_not_published_yet_is_its_own_answer_and_never_an_older_build(self):
        """The whole bug was a silent fallback. `_tag_for_commit` returning None must stop the
        bump, not quietly pin whatever happens to be newest."""
        self.assertIn("return None", BUMP[BUMP.index("def _tag_for_commit"):])
        main = BUMP[BUMP.index("def main()"):]
        self.assertIn("WAITING", main)
        self.assertIn("PosterChanOS will keep installing the PREVIOUS bundle", main)
        i = main.index("want = _tag_for_commit(head)")
        self.assertIn("if not want:", main[i:i + 200],
                      "a missing build must be handled immediately, not fall through")

    def test_a_pin_from_another_commit_is_called_out_even_in_the_default_mode(self):
        """Somebody running this without --for-commit still deserves to know the bundle they are
        about to publish is not the code they are looking at."""
        self.assertIn("not from HEAD", BUMP)
        self.assertIn("whatever the version number says", BUMP)

    def test_the_script_still_parses(self):
        r = subprocess.run(["python3", "-m", "py_compile", str(ROOT / "scripts" / "bump_desktop_overlay.py")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


class TheDeploySaysWhenTheDesktopIsNotOnIt(unittest.TestCase):
    def test_sync_checks_the_pin_against_the_commit_it_just_deployed(self):
        self.assertIn("bump_desktop_overlay.py --for-commit --check", SYNC)

    def test_it_prints_the_command_that_finishes_the_job(self):
        """A warning that does not say what to do next is a warning that gets ignored — and this one
        already cost a full day of "the fix is deployed" against a desktop that did not have it."""
        self.assertIn("scripts/bump_desktop_overlay.py --for-commit && ./scripts/publish_overlay.sh",
                      SYNC)

    def test_it_does_not_fail_the_deploy(self):
        """The server deploy really did succeed; blocking it on a build that takes ~15 minutes would
        trade a silent problem for a loud wrong one."""
        i = SYNC.index("bump_desktop_overlay.py --for-commit --check")
        block = SYNC[i:i + 900]
        self.assertNotIn("exit 1", block)

    def test_sync_sh_is_valid_shell(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "sync.sh")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
