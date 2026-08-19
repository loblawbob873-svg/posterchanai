"""The desktop bundle renders client.html ITSELF, so every template tag has to be known to it.

`desktop/build-www.sh` does not fetch a rendered page — it substitutes the Jinja tags locally and
then hard-fails on anything left over:

    build-www: unrendered template tag in client.html: {{ build }}

which is correct and is the only reason a half-rendered shell never ships. But it means adding ONE
tag to templates/client.html breaks every desktop platform at once, and nothing says so until CI —
mac, linux and windows each retried five times and went red, no new Windows app for hours, while the
Android build sailed through because it FETCHES an already-rendered page and never sees a raw tag.
That is exactly what `{{ build }}` did.

So: every `{{ … }}` in the template must be handled by the desktop script. Static, instant, and it
fails in the same second the tag is added instead of twenty minutes later on three runners.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates", "client.html")
DESKTOP = os.path.join(ROOT, "desktop", "build-www.sh")


class DesktopBundleRenders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TPL, encoding="utf-8") as fh:
            cls.tpl = fh.read()
        with open(DESKTOP, encoding="utf-8") as fh:
            cls.sh = fh.read()

    def test_every_template_tag_is_substituted_by_the_desktop_build(self):
        tags = sorted(set(re.findall(r"\{\{.*?\}\}", self.tpl, flags=re.S)))
        self.assertTrue(tags, "no tags found — the template moved, re-read this test")
        missing = []
        for t in tags:
            inner = t.strip("{} ").strip()
            name = re.split(r"[|\s(]", inner)[0]
            # Either the whole tag is replaced verbatim, or its NAME appears in a replace/sub call.
            if t in self.sh or ("'{{ %s }}'" % name) in self.sh or ('"{{ %s }}"' % name) in self.sh:
                continue
            if re.search(r"\{\{\s*%s\b" % re.escape(name), self.sh):
                continue
            missing.append(t)
        self.assertEqual(missing, [], "these tags reach the desktop bundle unrendered and it "
                                      "hard-fails on them, taking mac/linux/windows down together")

    def test_the_guard_that_catches_them_is_still_there(self):
        """Without it a half-rendered shell ships instead of failing — much worse than a red build."""
        self.assertIn("unrendered template tag", self.sh)

    def test_both_bundles_still_stamp_their_build(self):
        with open(os.path.join(ROOT, "mobile", "build-www.sh"), encoding="utf-8") as fh:
            mob = fh.read()
        for name, src in (("desktop", self.sh), ("mobile", mob)):
            self.assertIn("__PC_BUILD", src, "%s stopped stamping its commit" % name)


if __name__ == "__main__":
    unittest.main()
