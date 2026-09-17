"""Virtual Machines (and the desktop's other three screens) must open however you got there.

os.js builds four screens the app's render chain knows nothing about — __ossettings, __tasks,
__vms, __remote — and owns their renderers in EXTRA_RENDER. app.js consulted PCOS.renderExtra() in
exactly ONE place: routeFromPath(), behind `_inWin()`. A window opened AS a window therefore
worked, and every other way in — the start menu, a launcher tile, any switchView() — set VIEW and
painted nothing.

Reproduced on a real desktop before the fix: switchView('__vms') left VIEW === '__vms' with the
previous screen still on it, and on an empty feed it printed "Nothing here can show __vms".

These assertions are on the SHIPPED source and each fails if the hook is removed or narrowed back.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"
APP = ROOT / "app.js"
OS_JS = ROOT / "os.js"


def _code_only(src):
    """Remove /* … */ and // … comments, keeping offsets meaningful enough for ordering tests."""
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i))          # keep length so index ordering still means something
            i = j
        elif two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


class ExtraScreensOpen(unittest.TestCase):
    def setUp(self):
        # STRIP COMMENTS FIRST. The fix carries a long comment that names `renderExtra`,
        # `_inWin()` and `__vms`, so a search over the raw file matches the PROSE and the test
        # passes whatever the code does — the vacuous proof this repo keeps rediscovering.
        self.app = _code_only(APP.read_text())
        self.os = OS_JS.read_text()

    def test_the_render_chain_consults_renderExtra(self):
        """The regression: only routeFromPath() did, and it is gated on being a window."""
        # the fallthrough that printed "Nothing here can show <view>"
        fall = self.app.index("Nothing here can show")
        head = self.app[:fall]
        hook = head.rindex("PCOS.renderExtra")
        # the nearest _inWin() guard must not be what is wrapping this call
        between = head[hook:]
        self.assertNotIn("_inWin()", between,
                         "the only renderExtra before the fallthrough is still window-gated")
        self.assertLess(hook, fall,
                        "renderExtra is consulted after the 'nothing renders' fallthrough")

    def test_every_screen_os_owns_is_reachable(self):
        """A name in EXTRA_RENDER with no route here is a correctly-titled blank screen."""
        block = re.search(r"const EXTRA_RENDER = \{(.*?)\n  \};", self.os, re.S)
        self.assertIsNotNone(block, "EXTRA_RENDER not found in os.js")
        names = re.findall(r"'(__[a-z]+)'\s*:", block.group(1))
        self.assertIn("__tasks", names, "the Task Manager screen is no longer in EXTRA_RENDER")
        # `__vms` merged into the Virtual Machines view (`vms`, "This computer" first) — switchView maps it.
        self.assertNotIn("__vms", names)
        self.assertGreaterEqual(len(names), 3)
        # The hook keys on the leading underscore, so it must cover all of them.
        for n in names:
            self.assertTrue(n.startswith("_"),
                            "%s cannot be reached by the leading-underscore hook" % n)

    def test_the_hook_cannot_swallow_an_ordinary_view(self):
        """It must only claim the desktop's own names, never a real view."""
        fall = self.app.index("Nothing here can show")
        hook_at = self.app[:fall].rindex("PCOS.renderExtra")
        # Look back to the start of the enclosing block rather than a fixed number of characters —
        # a byte window silently stops covering the thing it checks the moment the block grows.
        block = self.app[:hook_at].rindex("if (VIEW")
        ctx = self.app[block:hook_at]
        self.assertRegex(ctx, r"VIEW\.charAt\(0\)\s*===\s*'_'",
                         "the hook is not restricted to the desktop's underscore-prefixed names")

    def test_an_incidental_repaint_does_not_tear_the_screen_down(self):
        """renderView is called INCIDENTALLY — an outbox flush, a relay reconnect — with no check on
        which view is open, and renderExtra reaches _paintExtraInFeed, whose contract is "TEAR THE
        PREVIOUS ONE DOWN FIRST". Unconditional, that kills and rebuilds a live Remote Desktop
        session and restarts Task Manager's polling every time a relay reconnects."""
        fall = self.app.index("Nothing here can show")
        hook_at = self.app[:fall].rindex("PCOS.renderExtra")
        block = self.app[self.app[:hook_at].rindex("if (VIEW"):hook_at]
        self.assertIn("_extraPaintedFor", block,
                      "the hook repaints an already-painted desktop screen on every incidental call")
        self.assertIn("_was", block,
                      "a repeat repaint does not restore what was on screen")

    def test_a_missing_desktop_is_not_an_exception(self):
        """In a browser tab or the APK there is no PCOS; the chain must fall through, not throw."""
        fall = self.app.index("Nothing here can show")
        hook_at = self.app[:fall].rindex("PCOS.renderExtra")
        ctx = self.app[hook_at - 200: hook_at + 200]
        self.assertIn("window.PCOS &&", ctx, "renderExtra is called without checking PCOS exists")
        self.assertIn("catch", ctx, "a throw from the desktop renderer escapes the render chain")


if __name__ == "__main__":
    unittest.main()
