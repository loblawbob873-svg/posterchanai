"""No resource name is defined twice, in any values file.

`mergeReleaseResources` fails with "Found item String/x more than one time" — a BUILD failure, so a
duplicate string does not merely shadow, it means no APK exists for that commit at all. That is the
most expensive kind of mistake here: the work is committed, pushed and green on every local test,
and there is simply nothing to install.

It happened on `sms_no_permission`, added by two sessions editing the same file minutes apart. No
local test looked at strings.xml, so the first sign was a red CI job on a commit whose code was fine.
"""
import re
import unittest
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "mobile/android/app/src/main/res"

# `<string name="x">`, `<string-array name="x">`, `<plurals name="x">`, `<color>`, `<dimen>` …
DECL = re.compile(r"<(string|string-array|plurals|color|dimen|bool|integer)\s+name=\"([^\"]+)\"")


class NoResourceIsDeclaredTwice(unittest.TestCase):
    def test_every_values_file_has_unique_names(self):
        files = sorted(RES.glob("values*/*.xml")) if RES.is_dir() else []
        self.assertTrue(files, "no values files found — the path moved and this stopped checking")
        for f in files:
            with self.subTest(file=f.name):
                # Per (kind, name): a `string` and a `dimen` may share a name, two strings may not.
                dupes = [k for k, n in Counter(DECL.findall(f.read_text())).items() if n > 1]
                self.assertEqual(dupes, [],
                                 "%s declares %r more than once — mergeReleaseResources fails and "
                                 "the commit produces no APK" % (f.name, dupes))


if __name__ == "__main__":
    unittest.main()
