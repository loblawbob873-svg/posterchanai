"""A popout window must not be claimed by desktop mode.

"🗔 Open in a window" opens ONE view — a stream — drawn without sidebar, nav or rightbar
(`body.popout`). It is the SAME ORIGIN as the tab that opened it, so it reads the same remembered
`osMode`, and `restore()` used to ask only two questions: is the desktop remembered, and is the
window wide enough. Neither is "am I a popout". So the windowed desktop took the window over, the
stream never drew, and with the chrome already hidden what was left was a bare desktop — reported
as "the Streams window button launches a new window with an empty desktop".

The size check cannot catch it. The popout is opened at `max(900, availWidth*0.7)`, which is 1344 on
a 1920-wide screen, comfortably over MIN_WIDTH (1024). It is also what makes the bug look
intermittent: at 1280 wide the popout is 900px, under MIN_WIDTH, and none of it happens.

  popout-is-left-alone      ?popout=1 + osMode remembered + a wide window → the desktop does NOT enter
  ordinary-window-restores  the same inputs WITHOUT popout → it does, or the guard has just disabled
                            desktop mode for everybody
  narrow-still-refused      the pre-existing size rule is untouched
  the-flag-is-not-cleared   the guard must RETURN, never write osMode:false — the flag is shared
                            with the tab that opened this window, so writing would exit desktop mode
                            over there too
  check-can-fail            with the guard removed, the popout IS claimed — so a pass means the
                            guard, not the harness

Runs the SHIPPED os.js under node, like tests/test_desktop_layout.py.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"
NODE = shutil.which("node") or shutil.which("nodejs")

# os.js is REQUIRED from a file, never inlined into `node -e`: it is ~180KB and the first version of
# this harness died with "Argument list too long".
#
# `enter()` is not stubbed, and does not need to be — it sets `on = true` (os.js:4364) BEFORE it
# touches the DOM, so `isOn()` reports the decision even though the stub document cannot survive the
# drawing that follows (and `restore()`'s own try/catch swallows that). This test is about whether
# restore() DECIDES to enter; scripts/check_os_desktop.py covers what entering draws.
HARNESS = """
const WRITES = [];
// BOTH `window.location` and the bare global. os.js reads `location.search` unqualified — the house
// style, same as app.js's own popout branch — which in a browser resolves through window. Defining
// only window.location left node throwing ReferenceError INSIDE the guard's try/catch, so the guard
// silently failed open and the test reported the bug as unfixed. Worth keeping in mind about the
// shipped code too: that catch means a `location` that ever fails to resolve enters desktop mode
// rather than refusing to.
global.location = { search: %(search)s };
global.window = { innerWidth: %(width)d,
                  location: global.location,
                  ClientSettings: { get:(k,d)=> (k==='osMode' ? %(osmode)s : d),
                                    set:(k,v)=>{ WRITES.push([k,v]); } } };
global.document = { addEventListener(){}, querySelector(){ return null; },
                    querySelectorAll(){ return []; }, getElementById(){ return null; },
                    createElement(){ return { style:{}, classList:{ add(){}, remove(){} },
                                              appendChild(){}, addEventListener(){} }; },
                    body:{ classList:{ add(){}, remove(){} }, appendChild(){} } };
global.getComputedStyle = () => ({ zoom: '1' });
require(%(mod)s);
const PCOS = window.PCOS;
PCOS.restore();
process.stdout.write(JSON.stringify({ on: PCOS.isOn(), writes: WRITES }));
"""


def _src(with_guard=True):
    src = OS_JS.read_text(encoding="utf-8")
    if not with_guard:
        # Remove exactly the popout early-return, leaving the rest of restore() as it ships.
        src, n = re.subn(
            r"\n\s*try\{ if\(new URLSearchParams\(location\.search\)\.get\('popout'\) === '1'\) return; \}catch\(_\)\{\}",
            "", src, count=1)
        assert n == 1, "could not find the popout guard to remove — it was renamed or moved"
    return src


@unittest.skipIf(not NODE, "no node on this node")
class PopoutIsNotADesktop(unittest.TestCase):
    def run_os(self, *, search, osmode=True, width=1344, with_guard=True):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="pc-osjs-") as d:
            mod = Path(d) / "os.js"
            mod.write_text(_src(with_guard), encoding="utf-8")
            drv = Path(d) / "drv.js"
            drv.write_text(HARNESS % {"width": width, "search": json.dumps(search),
                                      "osmode": "true" if osmode else "false",
                                      "mod": json.dumps(str(mod))}, encoding="utf-8")
            r = subprocess.run([NODE, str(drv)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-3000:])
        return json.loads(r.stdout)

    def test_popout_is_left_alone(self):
        """THE BUG. A stream popout on a 1920 screen, with desktop mode remembered."""
        o = self.run_os(search="?popout=1&e=naddr1abc")
        self.assertFalse(o["on"],
                         "desktop mode claimed a popout window — the stream never draws and what is "
                         "left is a bare desktop, because body.popout already hid the chrome")

    def test_ordinary_window_restores(self):
        """The other half, and the one that would make the guard a regression: a normal tab with
        desktop mode remembered must still get the desktop."""
        o = self.run_os(search="")
        self.assertTrue(o["on"], "the guard disabled desktop mode for ordinary windows")

    def test_narrow_still_refused(self):
        """The pre-existing size rule is untouched: MIN_WIDTH still wins."""
        o = self.run_os(search="", width=900)
        self.assertFalse(o["on"])

    def test_the_flag_is_not_cleared(self):
        """The popout shares localStorage with the tab that opened it. Writing osMode:false here
        would exit desktop mode over THERE — a much worse bug than the one being fixed."""
        o = self.run_os(search="?popout=1&e=naddr1abc")
        self.assertEqual([w for w in o["writes"] if w[0] == "osMode"], [],
                         "the popout wrote osMode — that flag is shared with the opener")

    def test_check_can_fail(self):
        o = self.run_os(search="?popout=1&e=naddr1abc", with_guard=False)
        self.assertTrue(o["on"],
                        "removing the guard did not reproduce the bug — the harness is not "
                        "exercising the path this test claims to cover")


if __name__ == "__main__":
    unittest.main()
