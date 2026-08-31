""""When a user opens the app, it should refresh." It did not.

The only catch-up was the mail poller's `visibilitychange` handler, and that is gated on the last
check already being POLL_MS — ten minutes — old. Right for flicking between browser tabs, wrong for
coming back to the app, which is the moment somebody is actually looking at it. And opening the
Email view refreshed nothing at all: `renderMailView` returns early whenever the list is already
mounted, which is the common case.

On a phone it was worse than a stale list. Everything else in this file is wired to Capacitor's
`resume` for a documented reason — a backgrounded WebView delivers `visibilitychange` late or
coalesces it away, while `resume` is fired by the Activity, which was never frozen. Mail was wired
to the unreliable signal only.

One freshness rule, `Mail.refreshIfStale`, so a resume, a widget press and opening the view cannot
disagree about what "fresh enough" means — and it refreshes BEHIND the cached list rather than in
front of it, which is the cache-first rule the rest of this client already follows.
"""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[2] / "static" / "js" / "client"
       / "app.js").read_text(encoding="utf-8")


class ComingBackToTheAppChecksMail(unittest.TestCase):
    def _fn(self) -> str:
        start = APP.index("    refreshIfStale(){")
        return APP[start:start + 900]

    def test_there_is_one_freshness_rule_and_it_is_short(self):
        fn = self._fn()
        self.assertIn("FRESH_MS", fn)
        m = re.search(r"FRESH_MS:\s*([0-9*\s]+),", APP)
        self.assertIsNotNone(m, "FRESH_MS is not declared")
        self.assertLessEqual(eval(m.group(1).strip()), 5 * 60 * 1000,
                             "a 'fresh enough' window this long is the bug it replaced")

    def test_it_refuses_the_cases_that_would_make_it_wrong_or_wasteful(self):
        fn = self._fn()
        for guard in ("GUEST", "navigator.onLine === false", "this._syncing",
                      "this.accounts && this.accounts.length"):
            self.assertIn(guard, fn, guard)

    def test_it_never_toasts_and_never_blocks(self):
        """A background refresh that toasts is a background refresh nobody wanted."""
        fn = self._fn()
        self.assertIn("this.sync(false)", fn)
        self.assertIn(".catch(()=>{})", fn)
        self.assertNotIn("await ", fn)
        self.assertNotIn("toast(", fn)

    def test_opening_the_view_refreshes_behind_the_list_it_already_has(self):
        view = APP[APP.index("function renderMailView(){"):]
        view = view[:view.index("function bumpMail()")]
        ask = view.index("Mail.refreshIfStale()")
        early = view.index("if(mounted && Mail.root===mounted) return;")
        self.assertLess(ask, early,
                        "the refresh sits after the early return, so a mounted list never refreshes")
        self.assertIn("// already up — never remount", view,
                      "the remount guard must stay — it owns scroll and the open message")

    def test_every_signal_that_means_the_app_came_back_checks_mail(self):
        """Three resume paths and the desktop's own wake signal. Wiring only one of them is how the
        timeline lost its subscription on Android — the same lesson, documented in this file."""
        self.assertIn("const _reMail=", APP)
        for site in ("{ _reShare(); _reMusic(); _reCal(); _reMail(); }",
                     "'resume', ()=>{ _reShare(); _reMusic(); _reCal(); _reMail(); _tlForeground();",
                     "if(st && st.isActive){ _reShare(); _reMusic(); _reCal(); _reMail(); _tlForeground();",
                     "onWake(()=>{ _hiddenAt = 0; _resumeRelay(); _reMail(); })"):
            self.assertIn(site, APP, site)

    def test_it_is_not_called_before_mail_exists(self):
        """`_reCal()` and `_reMusic()` are called at definition time. `Mail` is declared much further
        down this file, so doing the same here is a ReferenceError on every boot — swallowed by the
        try/catch, and therefore silent."""
        i = APP.index("const _reMail=")
        after = APP[i:i + 400]
        self.assertNotIn("\n      _reMail();", after,
                         "_reMail() is invoked immediately, before Mail is initialised")


if __name__ == "__main__":
    unittest.main()
