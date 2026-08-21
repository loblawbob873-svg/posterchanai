"""Every XML the Android build reads has to PARSE, and only CI ever found out.

    The whole APK build died at `:app:processReleaseMainManifest` with
    "MergeFailureException: Error parsing .../AndroidManifest.xml", five pushes in a row, ~90
    seconds in. The cause was two prose comments written in this repo's own house style:

        <!-- … "keeps its own ringer and ours is never asked" -- which is exactly right … -->

    A DOUBLE HYPHEN IS ILLEGAL INSIDE AN XML COMMENT. It ends the comment as far as the parser is
    concerned, and every XML parser refuses the file outright — so the manifest was not merely
    wrong in that comment, it was unreadable from the first `<`.

    Nothing here could see it. Fifteen test files read AndroidManifest.xml, and all of them read it
    as TEXT with regexes, which match happily inside a file no parser will accept. The Gradle build
    is the only thing that parses it and the Gradle build only runs in CI, so the answer arrived as
    a red workflow after every push, about a file that looked fine in every local test.

    This is the floor: parse them all, here, in milliseconds.
"""
import os
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android")
# Generated or vendored trees: gradle's own output, and the npm packages Capacitor copies in. A
# failure in either is not something an edit here can cause or fix.
SKIP = ("/build/", "/node_modules/", "/.gradle/", "/.idea/")


@unittest.skipIf(not os.path.isdir(ANDROID), "no Android project in this checkout")
class EveryAndroidXmlParses(unittest.TestCase):
    def _files(self):
        for base, dirs, names in os.walk(ANDROID):
            dirs[:] = [d for d in dirs if d not in ("build", "node_modules", ".gradle", ".idea")]
            for n in names:
                if n.endswith(".xml"):
                    p = os.path.join(base, n)
                    if not any(s in p.replace(os.sep, "/") for s in SKIP):
                        yield p

    def test_they_all_parse(self):
        found = list(self._files())
        self.assertGreater(len(found), 50, "the Android resources moved — re-point this test")
        for p in found:
            with self.subTest(os.path.relpath(p, ROOT)):
                try:
                    ET.parse(p)
                except ET.ParseError as e:
                    self.fail(f"{os.path.relpath(p, ROOT)} is not well-formed XML: {e}. "
                              "A `--` inside a <!-- comment --> is the usual cause here, and it "
                              "makes the whole file unreadable, not just that line.")

    def test_the_manifest_is_one_of_them(self):
        """Named on its own because it is the file the build dies on first, and because a walk that
        silently stopped finding anything would pass every assertion above it."""
        man = os.path.join(ANDROID, "app", "src", "main", "AndroidManifest.xml")
        self.assertIn(man, list(self._files()))
        ET.parse(man)
