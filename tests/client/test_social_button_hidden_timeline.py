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
        m = re.search(r"function activateNavView\(v\)\{.*?\n  \}", src, re.S)
        assert m, "activateNavView is gone from app.js"
        cls.activate = m.group(0)

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

    def _activate(self, pressed, active, hidden, start_pref):
        js = """
        const _TL_TABS = ['home','global','trending'];
        const ClientSettings = { get: (k, d) => k === 'startTimeline' ? %s : d };
        const tlHiddenSet = () => new Set(%s);
        const GUEST = false;
        let VIEW = %s;
        const events=[];
        const timelineTop=v=>events.push('top:'+v);
        const switchView=v=>events.push('view:'+v);
        %s
        %s
        activateNavView(%s);
        process.stdout.write(JSON.stringify(events));
        """ % (json.dumps(start_pref), json.dumps(hidden), json.dumps(active),
                 self.start_tl, self.activate, json.dumps(pressed))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_repeated_social_refreshes_its_configured_visible_timeline(self):
        """A hidden Social/Nostrverse button resolves to configured Home before deciding whether
        this is a repeated activation; the second tap must refresh, not navigate to the same view."""
        self.assertEqual(self._activate("global", "home", ["global"], "home"), ["top:home"])

    def test_visible_social_uses_the_configured_timeline_before_refreshing(self):
        self.assertEqual(self._activate("global", "home", [], "home"), ["top:home"])
        self.assertEqual(self._activate("global", "global", [], "home"), ["view:home"])
        self.assertEqual(self._activate("global", "global", [], "global"), ["top:global"])

    def _double_activate(self, active, hidden, start_pref):
        """Execute both presses; switchView mutates VIEW as the shipped router does."""
        js = """
        const _TL_TABS = ['home','global','trending'];
        const ClientSettings = { get: (k, d) => k === 'startTimeline' ? %s : d };
        const tlHiddenSet = () => new Set(%s);
        const GUEST = false;
        let VIEW = %s;
        const events=[];
        const timelineTop=v=>events.push('top:'+v);
        const switchView=v=>{events.push('view:'+v);VIEW=v;};
        %s
        %s
        activateNavView('global');
        activateNavView('global');
        process.stdout.write(JSON.stringify(events));
        """ % (json.dumps(start_pref), json.dumps(hidden), json.dumps(active),
                 self.start_tl, self.activate)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_double_social_from_another_view_opens_then_refreshes_configured_home(self):
        """Exact phone gesture: Messages → Social → Social with Nostrverse hidden."""
        self.assertEqual(self._double_activate("messages", ["global"], "home"),
                         ["view:home", "top:home"])

    def test_double_social_uses_configured_home_even_when_nostrverse_is_visible(self):
        self.assertEqual(self._double_activate("messages", [], "home"),
                         ["view:home", "top:home"])

    def test_double_social_never_targets_a_hidden_saved_timeline(self):
        """A stale Home preference falls through to the remaining visible Trending timeline."""
        self.assertEqual(self._double_activate("messages", ["global", "home"], "home"),
                         ["view:trending", "top:trending"])


if __name__ == "__main__":
    unittest.main()
