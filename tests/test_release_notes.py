"""Release notes for the Zapstore listing — generated, not hand-written.

Zapstore publishing has been automated since it was set up, but every release went out with NO
NOTES: a version number and nothing about what changed. `zsp` reads them from the `release_notes:`
path in zapstore.yaml, so CI writes that file from the commits that are actually in the build.

The judgement being tested is what a user should see. This repo's commit subjects are written as
sentences about behaviour, which makes them unusually good release-note material — but a build also
contains work nobody outside should read: tests, CI, docs, the OS installer. Those are dropped by
looking at what a commit TOUCHED, not by matching words in it, because a subject line is prose and
the paths are fact.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "release_notes.py")


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=60)


class ReleaseNotes(unittest.TestCase):
    """Driven against a REAL throwaway repository, because the whole rule is about which files a
    commit touched — a fake git log cannot exercise it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        git("init", "-q", "-b", "main", cwd=self.dir)
        git("config", "user.email", "t@example.com", cwd=self.dir)
        git("config", "user.name", "T", cwd=self.dir)
        os.makedirs(os.path.join(self.dir, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "static", "js", "client"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "tests"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "app", "services"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "os"), exist_ok=True)

    def commit(self, path, subject):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a") as fh:
            fh.write("x\n")
        git("add", "-A", cwd=self.dir)
        git("commit", "-q", "-m", subject, cwd=self.dir)
        return git("rev-parse", "HEAD", cwd=self.dir).stdout.strip()

    def run_notes(self, since="", version=""):
        r = subprocess.run([sys.executable, SCRIPT, "--since", since, "--version", version,
                            "--root", self.dir, "--out", "RN.md"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        with open(os.path.join(self.dir, "RN.md"), encoding="utf-8") as fh:
            return fh.read()

    def test_it_lists_what_shipped_in_the_apk(self):
        self.commit("static/js/client/app.js", "Reader mode remembers where you were")
        out = self.run_notes()
        self.assertIn("- Reader mode remembers where you were", out)

    def test_it_leaves_out_what_never_reaches_a_phone(self):
        """A build full of test and server work is not a build with nothing in it — but none of it
        belongs in an app store listing."""
        self.commit("tests/test_thing.py", "Cover the reconcile table")
        self.commit("app/services/relay.py", "Relay prune keeps calendars")
        self.commit("os/gentoo.sh", "PosterChanOS profile")
        out = self.run_notes()
        for gone in ("Cover the reconcile", "Relay prune", "PosterChanOS profile"):
            self.assertNotIn(gone, out, f"{gone!r} is not something an APK user can see")

    def test_a_release_with_nothing_user_facing_says_so(self):
        """It happens, and it is a real outcome. Inventing a line for it is worse than saying it."""
        self.commit("tests/test_thing.py", "More coverage")
        out = self.run_notes()
        self.assertIn("no user-facing changes", out.lower())

    def test_the_headline_is_kept_and_the_reasoning_dropped(self):
        """Subjects here read "headline — why". The store gets the headline; the why is for the
        commit log."""
        self.commit("static/js/client/sync.js",
                    "Bytes this device already holds are not downloaded again — the mirror of "
                    "the send side's settle-by-content")
        out = self.run_notes()
        self.assertIn("- Bytes this device already holds are not downloaded again\n", out)

    def test_a_very_long_subject_is_cut_at_a_boundary_not_mid_word(self):
        self.commit("mobile/android/x.java",
                    "Resume for a file with no checksum, a stall window that fits the chunk, and "
                    "say when a delete is waiting")
        out = self.run_notes()
        line = [l for l in out.splitlines() if l.startswith("- ")][0]
        self.assertLessEqual(len(line), 92, line)
        self.assertFalse(line.rstrip().endswith("-"), line)
        self.assertNotIn("  ", line)

    def test_it_starts_after_the_given_commit(self):
        old = self.commit("static/js/client/app.js", "Old thing nobody should see again")
        self.commit("static/js/client/app.js", "New thing")
        out = self.run_notes(since=old)
        self.assertIn("New thing", out)
        self.assertNotIn("Old thing", out)

    def test_a_sha_that_is_not_here_falls_back_instead_of_failing(self):
        """CI reads the marker out of the previous release's body. A rewritten history, a fork, or a
        first-ever run all produce a sha this repository does not have — and a release must not fail
        because its notes could not pick a start point."""
        self.commit("static/js/client/app.js", "Something visible")
        out = self.run_notes(since="0" * 40)
        self.assertIn("Something visible", out)

    def test_duplicate_subjects_appear_once(self):
        self.commit("static/js/client/app.js", "Same fix twice")
        self.commit("static/js/client/app.js", "Same fix twice")
        out = self.run_notes()
        self.assertEqual(out.count("Same fix twice"), 1, out)

    def test_it_records_which_commit_it_was_built_from(self):
        """The footer is how the NEXT build knows where to start."""
        sha = self.commit("static/js/client/app.js", "A change")
        out = self.run_notes()
        self.assertIn(sha[:7], out)

    def test_the_version_heads_the_notes(self):
        self.commit("static/js/client/app.js", "A change")
        out = self.run_notes(version="1.0.1300")
        self.assertTrue(out.startswith("## 1.0.1300"), out[:40])


class WiredIntoPublishing(unittest.TestCase):
    def test_zapstore_points_at_the_generated_file(self):
        with open(os.path.join(ROOT, "zapstore.yaml"), encoding="utf-8") as fh:
            y = fh.read()
        self.assertIn("release_notes: ./RELEASE_NOTES.md", y,
                      "zsp reads notes from release_notes: — without it every release is blank")

    def test_the_notes_are_not_committed(self):
        """They describe ONE build. A stale copy in the tree would be published as though it
        described this one."""
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("RELEASE_NOTES.md", fh.read())

    def test_ci_generates_them_before_it_publishes(self):
        with open(os.path.join(ROOT, ".github", "workflows", "android.yml"), encoding="utf-8") as fh:
            w = fh.read()
        self.assertLess(w.index("release_notes.py"), w.index("zsp publish"),
                        "the notes are written after the publish that was supposed to read them")

    def test_the_marker_is_read_before_the_release_is_deleted(self):
        """The window comes from the previous release's body, and the workflow DELETES that release
        to re-date it. Read afterwards, the marker is always missing and every build would fall back
        to a fixed window — quietly, since the fallback works."""
        with open(os.path.join(ROOT, ".github", "workflows", "android.yml"), encoding="utf-8") as fh:
            w = fh.read()
        self.assertLess(w.index("built-from: [0-9a-f]"), w.index("gh release delete apk-latest"),
                        "the previous build marker is read after the release holding it is deleted")


if __name__ == "__main__":
    unittest.main()
