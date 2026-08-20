"""Go Live is not offered on a bundle with no instance behind it.

    "I tried to go Live on PosterCHanOS nothing happened"

Measured on the machine: the desktop config holds no instance, so it runs standalone. Go Live is
entirely server-backed — the RTMP/WHIP ingest lives on the instance — so the entry could only ever
reach `_goLive`'s "couldn't reach the server" toast. From the outside that is a button that does
nothing.

The gap is structural rather than a missed line. `applyInstanceGating` hides server-backed VIEWS via
`_viewNeedsInstance`, and the desktop launcher's extras — Music, Go Live — are ACTIONS with an id and
no `data-view`, which app.js already notes elsewhere. They are never in that set, so nothing was ever
going to hide them; each has to state its own requirement.

`standalone` is exposed as a PREDICATE, not a resolved boolean, because an instance can be set and
cleared while the app is running.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static/js/client/app.js"
OS_JS = ROOT / "static/js/client/os.js"


class TheDesktopEntryChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.os = OS_JS.read_text()
        i = cls.os.index("view: '__golive'")
        cls.entry = cls.os[i:cls.os.index("},", i)]

    def test_it_requires_an_instance(self):
        self.assertIn("standalone", self.entry,
                      "Go Live is offered on a desktop with no server to publish to")

    def test_it_is_asked_not_remembered(self):
        """A constant captured at load is wrong the moment somebody sets an instance."""
        self.assertIn("PC().standalone()", self.entry)

    def test_it_still_requires_being_signed_in(self):
        self.assertIn("me()", self.entry)


class TheSidebarEntryChecks(unittest.TestCase):
    def test_nav_golive_requires_an_instance(self):
        src = APP.read_text()
        i = src.index("$('#nav-golive')")
        self.assertIn("_standalone()", src[i:i + 200],
                      "the sidebar offers Go Live with no server behind it")

    def test_the_setting_is_still_honoured(self):
        src = APP.read_text()
        i = src.index("$('#nav-golive')")
        self.assertIn("stream_enabled", src[i:i + 200])


class ThePredicateIsReachable(unittest.TestCase):
    """os.js is a separate module; a function that is not on the surface is the `PC._fmtBytes is not
    a function` trap, and here it would fail OPEN — `PC().standalone` undefined means the guard
    passes and the entry comes back."""

    def test_standalone_is_on_the_pc_surface(self):
        # Brace-matched, not a fixed window: the surface literal is over 10KB and an 8000-character
        # slice reported this missing while it was three lines further down.
        src = APP.read_text()
        i = src.index("window.__PC = {")
        depth, k = 0, src.index("{", i)
        while True:
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        self.assertIn("standalone", src[i:k + 1])

    def test_the_call_site_tolerates_an_older_surface(self):
        """A bundled app.js and a newer os.js can be mismatched during a partial deploy."""
        src = OS_JS.read_text()
        i = src.index("view: '__golive'")
        self.assertIn("PC().standalone &&", src[i:i + 400])


if __name__ == "__main__":
    unittest.main()
