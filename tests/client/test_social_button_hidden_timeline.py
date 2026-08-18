"""The Social button must never land on a timeline the user has hidden.

Reported: "Clicking the social feed opens to nostrverse even though I have that feed hidden and in
the settings configured the app to open to home." The Social entry (sidebar + bottom bar) is
hardcoded data-view="global"; the fix resolves any HIDDEN timeline through _startTimeline() inside
switchView — the single entry every button, deep link and restored view comes through.

The shipped normalisation head of switchView and the shipped _startTimeline are CUT out of app.js
and RUN here (a copy would keep passing after the original changed)."""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _slice(src, start, end, what):
    a = src.index(start)
    b = src.index(end, a)
    assert b > a, what
    return src[a:b]


@unittest.skipIf(not NODE, "no node on this node")
class SocialButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = open(APP, encoding="utf-8").read()
        # the pure decision head of switchView: everything before the PCOS window routing
        head = _slice(src, "function switchView(v, quiet){", "/* PosterChan OS:",
                      "switchView head")
        cls.head = head + "\nreturn v; }"
        m = re.search(r"function _startTimeline\(\)\{.*?\n  \}", src, re.S)
        assert m, "_startTimeline is gone from app.js"
        cls.start_tl = m.group(0)

    def _route(self, view, hidden, start_pref, guest=False):
        js = """
        const window = { PC_NOSTR_ONLY: false };
        const _TL_TABS = ['home','global','trending'];
        const GUEST = %s;
        const ClientSettings = { get: (k, d) => k === 'startTimeline' ? %s : d };
        const tlHiddenSet = () => new Set(%s);
        const _viewNeedsInstance = () => false;
        let _onLandingView = true;
        %s
        %s
        process.stdout.write(JSON.stringify(switchView(%s)));
        """ % (json.dumps(guest), json.dumps(start_pref), json.dumps(hidden),
               self.start_tl, self.head, json.dumps(view))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_the_reported_case_social_goes_home(self):
        """global hidden + open-on-home → the Social button lands on Home."""
        self.assertEqual(self._route("global", ["global"], "home"), "home")

    def test_a_visible_tab_is_left_alone(self):
        self.assertEqual(self._route("global", [], "home"), "global")
        self.assertEqual(self._route("trending", ["global"], "home"), "trending")

    def test_hidden_with_no_landing_pref_falls_to_the_first_visible(self):
        self.assertEqual(self._route("global", ["global"], "global"), "home")

    def test_the_landing_pref_itself_hidden_still_resolves(self):
        self.assertEqual(self._route("home", ["home"], "home"), "global")

    def test_non_timeline_views_are_untouched(self):
        self.assertEqual(self._route("messages", ["global"], "home"), "messages")


if __name__ == "__main__":
    unittest.main()
