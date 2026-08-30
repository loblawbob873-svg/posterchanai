"""The signing guard still catches a NEW key, and stops crying about the retired one.

The Android signing key that was exposed has been ROTATED — docs/ZAPSTORE_SIGNING_RECOVERY.md. It
is not used for releases, not loaded into CI, and current APKs carry Android's signed
proof-of-rotation. The mainline was purged: it is reachable from neither HEAD, origin/master nor
github/main. What keeps it reachable at all is published release tags on the public mirror, and
rewriting those to delete a dead key would break download links people hold.

So the incident was closed by rotation, and the guard now accounts for that ONE object by id. This
file exists to make sure that concession stays exactly one object wide: a new key committed to the
same path is a different blob and must still fail the check.
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_no_android_signing_history.py"
sys.path.insert(0, str(ROOT / "scripts"))
import check_no_android_signing_history as guard  # noqa: E402


class TheGuardIsExactlyOneObjectWide(unittest.TestCase):
    def test_the_repository_passes_with_the_retired_key_accounted_for(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_concession_names_one_object_and_it_is_a_full_object_id(self):
        self.assertEqual(len(guard.RETIRED_SIGNING_OBJECTS), 1,
                         "the retired-key allowance grew; each entry must be justified in the doc")
        for oid in guard.RETIRED_SIGNING_OBJECTS:
            self.assertRegex(oid, r"^[0-9a-f]{40}$",
                             "pinned by full object id, so it cannot match anything else")

    def test_a_different_signing_container_still_fails(self):
        """A NEW key committed to the same path is a different blob. If this ever passes, the
        check has stopped protecting anything."""
        repo = Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True,
                                   check=True).stdout.strip())
        try:
            run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                            capture_output=True, text=True)
            run("init", "-q")
            run("config", "user.email", "t@t"); run("config", "user.name", "t")
            (repo / "mobile").mkdir()
            (repo / "mobile" / "release.keystore").write_bytes(b"not the retired key")
            run("add", "-A"); run("commit", "-qm", "add a key")
            # …and delete it, the way the real repo did: history still carries it.
            (repo / "mobile" / "release.keystore").unlink()
            run("add", "-A"); run("commit", "-qm", "remove it")
            found = guard.reachable_private_signing_paths(repo)
            self.assertTrue(found, "a signing container removed by a later commit went unnoticed")
            self.assertNotIn("684f66c811c5829034f6c413b2cc937dc37583cc",
                             {oid for oid, _ in found})
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)
