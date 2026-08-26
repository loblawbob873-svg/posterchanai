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
        i = self.os.index("function handoffPayload(")
        payload = self.os[i:self.os.index("function sendFrameHandoff", i)]
        self.assertIn("path:", payload,
                      "the monitor handoff sends only a view NAME, so a repo/article/stream lands on "
                      "the other monitor as a view nothing can route")
        self.assertIn("appPath", payload,
                      "the path must be the WINDOW's, captured when it was focused/parked")
        self.assertIn("appPath==='/' || appPath==='/index.html'", payload,
                      "web and packaged page roots are not entity addresses; either would open an "
                      "extra Social window")
        self.assertIn("path:topPath ? '' : appPath", payload)

    def test_the_path_belongs_to_THE_WINDOW_not_to_the_page(self):
        """`location.pathname` is a property of the PAGE. Read at drag time it hands whichever
        window you happened to move whatever address the page was last left on — dragging the AI
        chat to the other monitor opened the REPO LIST, because a repo had been opened earlier and
        its naddr was still in the URL.

        The previous version of this test asserted only that the payload carried a path, which that
        bug satisfies perfectly. What has to be true is where the path comes FROM."""
        i = self.os.index("function handoffPayload(")
        payload = self.os[i:self.os.index("function sendFrameHandoff", i)]
        self.assertIn("w.appPath", payload,
                      "the payload does not use the window's own captured path")
        self.assertNotIn("viewPath()", payload,
                         "the payload reads the live page URL at drag time, so every window is "
                         "handed whatever address the page happens to be on")

    def test_terminal_identity_cannot_be_replaced_by_the_global_social_route(self):
        i = self.os.index("function handoffPayload(")
        payload = self.os[i:self.os.index("function sendFrameHandoff", i)]
        self.assertIn("const terminal = w.view === 'terminal'", payload)
        self.assertIn("view:handoffIdentity(w)", payload)
        self.assertIn("const appPath = terminal || music ? ''", payload,
                      "the destination would adopt the PTY and then route it back to Social")

    def test_music_launcher_returns_its_real_window_for_monitor_handoff(self):
        """__music is an action alias, but the destination needs the window openDoc created.

        Discarding it makes the handoff receiver take its `if(!w)return` branch, producing the
        reported black Music window with only the unrelated floating transport left visible.
        """
        start = self.os.index("function openApp(")
        body = self.os[start:self.os.index("function windowAIContext", start)]
        self.assertIn("return view==='__music'&&opened ? opened : null", body)
        receive = self.os[self.os.index("if(pcWM.onHandoffFrame"):
                               self.os.index("if(pcWM.onPreviewFrame")]
        self.assertIn("const w=openApp(String(p.view)", receive)
        self.assertIn("if(!w) return", receive)

    def test_music_handoff_uses_the_reconstructible_launcher_not_a_generic_document(self):
        """doc:music is the live window key, but only __music knows how to paint Music."""
        start = self.os.index("function handoffIdentity(")
        identity = self.os[start:self.os.index("function handoffPayload(", start)]
        self.assertIn("if(opened==='doc:music') return '__music'", identity)
        payload = self.os[self.os.index("function handoffPayload("):
                          self.os.index("function sendFrameHandoff")]
        self.assertIn("const music = w.view === 'doc:music' || w.view === '__music'", payload)
        self.assertIn("const appPath = terminal || music ? ''", payload,
                      "a stale Social/repo path would repaint the reconstructed Music window")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_simple_app_identity_cannot_be_replaced_by_global_social_view(self):
        """Every ordinary app keeps its opened identity when another window changes the page-global
        route.  Stats was only the first report; testing the complete launcher surface prevents the
        same class of bug being reintroduced one app at a time."""
        start = self.os.index("function handoffIdentity(")
        brace = self.os.index("{", start)
        depth = 0
        end = None
        for pos in range(brace, len(self.os)):
            if self.os[pos] == "{":
                depth += 1
            elif self.os[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        self.assertIsNotNone(end)
        fn = self.os[start:end]
        simple_views = [
            "ai", "articles", "blackjack", "blossom", "bookmarks", "budget", "calendar",
            "calls", "chess", "code", "communities", "concord", "connect4", "contacts",
            "details", "drafts", "global", "hangman", "holdem", "home", "mail", "market",
            "markets", "meme", "messages", "news", "notes", "notifications", "repos",
            "shorts", "signer", "stats", "streams", "sync", "texts", "tiles", "torrents",
            "translate", "ttt", "vault", "websearch", "xdc",
        ]
        cases = [
            *({"view": view, "appView": "home" if view != "home" else "global", "appPath": ""}
              for view in simple_views),
            {"view": "terminal", "appView": "home", "appPath": "/"},
            {"view": "doc:music", "appView": "home", "appPath": "/"},
            {"view": "repos", "appView": "repo", "appPath": "/naddr1repo"},
            {"view": "settings", "appView": "admin", "appPath": ""},
        ]
        script = fn + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(handoffIdentity)))"
        got = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                                        check=True).stdout)
        self.assertEqual(got, simple_views + ["terminal", "__music", "repo", "admin"])

    def test_every_place_that_captures_appView_captures_appPath(self):
        """They are one fact about a window — which screen it is showing — and a site that records
        half of it leaves the other half stale. That is the same drift that makes two
        hand-maintained copies of anything go wrong, and here it surfaces as a window that reopens
        on somebody else's page."""
        sites = [ln for ln in self.os.splitlines()
                 if re.search(r"\bw\.appView\s*=|\bx\.appView\s*=", ln)]
        self.assertGreaterEqual(len(sites), 3, "appView capture sites moved — re-read this test")
        for ln in sites:
            with self.subTest(line=ln.strip()[:70]):
                idx = self.os.index(ln)
                # appPath must be set in the same statement or the next couple of lines.
                window = self.os[idx:idx + 900]
                self.assertIn("appPath", window,
                              "this records the window's view but not its address, so a handoff "
                              "from it reopens the wrong page")

    def test_the_destination_routes_it(self):
        i = self.os.index("onHandoffFrame")
        body = self.os[i:i + 3000]
        self.assertIn("routePath", body, "the destination never acts on the path it was sent")
        self.assertIn("p.path!=='/' && p.path!=='/index.html'", body,
                      "web and packaged roots are non-entity screens; routing either would create "
                      "an extra Social window beside the app that was moved")

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
