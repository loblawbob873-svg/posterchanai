"""Moving a window to the other monitor must reopen what it was SHOWING, not spin for ever.

Run: venv-unified/bin/python -m pytest tests/test_monitor_handoff_reopens.py

Reported as "moving git from one monitor to the other makes git infinite load in a black screen with
circle". Two monitors are two Electron renderers, so no DOM and no module state crosses the seam —
the destination rebuilds the window from the payload alone. The payload carried `w.appView`, which
is the LIVE view, and git.js sets `S.VIEW='repo'` the moment you open a repo. `renderView` has no
branch for `repo` (it is reached only through `openRepo(event)`), and that chain is
`if(VIEW===x) return render_x()` all the way down and then simply ENDS — so the spinner it painted
at the top stood for ever. Nothing threw, nothing logged, and the app "never loaded".

Three things are asserted, and each one alone leaves the bug:

  * renderView must never end having left a spinner it cannot replace. This is the CLASS fix: any
    module that names a sub-view (git does; an article, a stream, a listing, a thread all could)
    would otherwise produce the same dead screen.
  * the handoff payload must carry the PATH — the address the screen already publishes for itself
    (openRepo calls `_navUrl('/'+naddr)`), which routeFromPath already routes for every shared link.
  * desktop/main.js must pass it through. That sanitiser is an ALLOWLIST: a field the renderer sends
    and it does not name is dropped in silence, and the payload still arrives looking complete.

And the same-origin rule on that path is checked in all three places at once, against the same
cases — three hand-maintained copies of a rule is how one of them ends up wrong.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
OS_JS = os.path.join(ROOT, "static", "js", "client", "os.js")
MAIN = os.path.join(ROOT, "desktop", "main.js")
NODE = shutil.which("node")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class HandoffReopensWhatItWasShowing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.os = _read(OS_JS)
        cls.main = _read(MAIN)

    # ── the class fix ───────────────────────────────────────────────────────────────────────
    def test_render_view_never_ends_leaving_a_spinner(self):
        """A spinner is a promise that something is coming. Where nothing is, the promise must be
        withdrawn — not left standing, which is indistinguishable from a hung app."""
        i = self.app.index("function renderView(")
        j = self.app.index("\n  // ---------- timeline ----------", i)
        body = self.app[i:j]
        self.assertIn("feed.innerHTML = '<div class=\"spinner\"></div>'", body,
                      "renderView no longer paints the spinner this guard is about — re-read this test")
        tail = body[body.rindex("VIEW==='profile'"):]
        self.assertIn("innerHTML", tail,
                      "renderView still falls off the end without replacing the spinner, so any view "
                      "no branch handles is a permanent black screen with a circle")
        self.assertIn("switchView('home')", tail,
                      "the dead-end must offer a way out, not just an apology")

    # ── the payload ─────────────────────────────────────────────────────────────────────────
    def test_the_handoff_carries_the_path(self):
        # Anchored on the payload LITERAL and read forward to the call — the first mention of
        # `pcWM.handoffFrame` in this file is inside a comment, and searching back from it lands
        # before the object exists.
        i = self.os.index("const payload={")
        payload = self.os[i:self.os.index("pcWM.handoffFrame(payload", i)]
        self.assertIn("path:", payload,
                      "the monitor handoff sends only a view NAME, so a repo/article/stream lands on "
                      "the other monitor as a view nothing can route")
        self.assertIn("viewPath", payload)

    def test_the_destination_routes_it(self):
        i = self.os.index("onHandoffFrame")
        body = self.os[i:i + 3000]
        self.assertIn("routePath", body, "the destination never acts on the path it was sent")
        self.assertIn("if(p.path)", body,
                      "a bare '/' is every non-entity screen; routing it unconditionally would throw "
                      "the window back to Social")

    def test_the_client_exposes_both_halves(self):
        """os.js can only reach what is on `window.__PC` — the recurring `PC.x is not a function`."""
        i = self.app.index("ensureProfile: _ensureProfile")
        surface = self.app[i:i + 3000]
        self.assertIn("viewPath:", surface)
        self.assertIn("routePath:", surface)

    # ── the silent-drop trap ────────────────────────────────────────────────────────────────
    def test_the_ipc_allowlist_passes_the_path_through(self):
        """`pc:wm:handoff-frame` rebuilds the payload field by field. One this does not name is
        dropped with no error at either end."""
        i = self.main.index("ipcMain.handle('pc:wm:handoff-frame'")
        body = self.main[i:i + 2200]
        self.assertIn("path:", body,
                      "desktop/main.js drops the path on the floor — the renderer sends it, the "
                      "destination never sees it, and nothing says so")

    # ── one rule, three copies, checked together ────────────────────────────────────────────
    def _rules(self):
        """Every same-origin-path test in the codebase, lifted from its own source."""
        out = {}
        for name, src in (("main.js", self.main), ("app.js", self.app)):
            m = re.search(r"/\^\\/\(\?\!\\/\)/", src)
            self.assertTrue(m, f"{name} no longer guards the path shape — re-read this test")
            out[name] = r"/^\/(?!\/)/"
        return out

    @unittest.skipUnless(NODE, "node is not installed")
    def test_a_protocol_relative_path_is_refused_everywhere(self):
        """It is handed to history.replaceState, where `//host` is a different origin wearing a
        path's clothes."""
        rules = self._rules()
        cases = ["/naddr1abc", "/", "//evil.example/x", "https://evil.example",
                 "/x?tab=files", "", "\\\\evil.example"]
        script = ("const re=%s;\n"
                  "console.log(JSON.stringify(%s.map(c => re.test(c))));"
                  % (list(rules.values())[0], json.dumps(cases)))
        got = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                                        check=True).stdout)
        self.assertEqual(got, [True, True, False, False, True, False, False],
                         "the same-origin path rule accepts something it must not")
        # And every copy of it is the same rule.
        self.assertEqual(len(set(rules.values())), 1,
                         "the path rule has drifted between app.js and desktop/main.js")


if __name__ == "__main__":
    unittest.main()
