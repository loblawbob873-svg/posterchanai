"""`body.anim-off` may stop the client's decoration. It may never hide a post.

Run: venv-unified/bin/python -m unittest tests.client.test_anim_off_never_hides_content
     (the browser half needs google-chrome-stable; it skips itself if there is no browser)

THE BUG THIS EXISTS FOR — "every time I resume the android app, the timeline is empty with
REPLYING TO... posts, nothing loads for a while."

Two shipped rules met and produced a screen nothing could diagnose:

  body.anim-off *{ animation-play-state: paused }      pause decoration while backgrounded
  .note{ animation: noteRise .3s ... both }            cards rise in; the rise STARTS at opacity 0

A card inserted while the class is on is frozen at its first keyframe — fully transparent, at its
full height, for as long as the class stays on. `.reply-ctx` carries no animation, so the label
renders perfectly above the post it belongs to: "↩ REPLYING TO alice" with nothing underneath, all
the way down, and plain posts simply absent. The local relay was working the whole time and the
cache-first paint was drawing the right posts; they were painted invisible.

It survived the ghost-pair sweep because that sweep asks "is the <article> missing?" and "is it zero
height?" — this card is present and 46px tall. It is measured here as OPACITY, which is the thing
that was actually wrong.

And the class was left on because it was armed and released from `visibilitychange` alone, the one
signal this client already documents as arriving late on Android or being coalesced away entirely.
The resume path draws INSIDE that gap: Capacitor's `resume` fires from the Activity (never frozen),
_tlForeground re-subscribes and _resumeRelay repaints — all while the WebView still says hidden.
"Nothing loads for a while" is the delayed visibilitychange finally landing and releasing them.

So there are two assertions and they are not the same one:

  visible      run the REAL client.css: a card drawn under `anim-off` can be seen
  one owner    `anim-off` is written only by _animOff, which _tlBackground/_tlForeground call — so it
               cannot be armed from both signals and released from only one again
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPJS = os.path.join(REPO, "static", "js", "client", "app.js")
CSS = os.path.join(REPO, "static", "css", "client.css")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


APP = _read(APPJS)


# The feed as feedNoteHtml builds it: a reply is a .reply-pair wrapping the label AND the card, and a
# top-level post is a bare card. Both halves matter — the label is what stays visible and makes the
# screen read as "REPLYING TO with no posts" rather than as a blank page.
PAGE = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="client.css">
<body class="anim-off">
<div id="tl-notes"></div>
<pre id="out"></pre>
<script>
document.getElementById('tl-notes').innerHTML =
   '<div class="reply-pair" data-key="a"><div class="reply-ctx">'
 + '<span class="reply-ctx-lbl">\\u21a9 replying to <span class="name">alice</span></span></div>'
 + '<article class="note" data-id="a"><div class="body">a reply body</div></article></div>'
 + '<article class="note" data-id="b"><div class="body">an ordinary top-level post</div></article>';
/* Read on a TIMER, not on requestAnimationFrame: headless-with-dump-dom never presents a frame, so
   the rAF callback does not run and the report comes back empty — a page that reports nothing would
   fail every assertion here for a reason that has nothing to do with the stylesheet. The delay is
   what matters (an entry animation of .25-.3s must have started, and a frozen one must have been
   given the chance to unfreeze), and a timer is what virtual time advances. */
setTimeout(() => {
  const g = e => ({opacity: getComputedStyle(e).opacity, height: e.offsetHeight});
  const [a, b] = document.querySelectorAll('article.note');
  document.getElementById('out').textContent = JSON.stringify({
    replyCard: g(a), plainCard: g(b), label: g(document.querySelector('.reply-ctx')),
  });
}, 600);
</script>
"""


def _render(css_text):
    """Load a real stylesheet in a real browser and report what the feed looks like under anim-off."""
    import json

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "client.css"), "w", encoding="utf-8") as fh:
            fh.write(css_text)
        page = os.path.join(tmp, "feed.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(PAGE)
        res = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=2500",
             f"--user-data-dir={os.path.join(tmp, 'profile')}", "--dump-dom", "file://" + page],
            capture_output=True, text=True, timeout=90)
        assert res.returncode == 0, res.stderr[-2000:]
        m = re.search(r'<pre id="out">(\{.*?\})</pre>', res.stdout, re.S)
        assert m, "the page never reported — see the dump:\n" + res.stdout[:2000]
        return json.loads(m.group(1))


@unittest.skipUnless(CHROME, "needs Chrome to resolve real CSS")
class BackgroundedFeedIsStillVisible(unittest.TestCase):
    def test_a_card_drawn_while_backgrounded_can_be_seen(self):
        """The regression test. Every post inserted under `anim-off` must be visible."""
        got = _render(_read(CSS))
        for name in ("replyCard", "plainCard"):
            self.assertGreater(
                float(got[name]["opacity"]), 0,
                f"{name} is transparent under body.anim-off: {got[name]} — a post drawn while the "
                f"app is backgrounded is invisible for as long as the class stays on. "
                f"client.css must switch animations OFF (animation:none), never FREEZE them "
                f"(animation-play-state:paused), which strands an entry animation at opacity 0.")

    def test_the_label_and_its_post_agree(self):
        """The shape of the report: the label was visible and the post under it was not.

        Asserted separately because a stylesheet that hid BOTH would be a different (and far more
        obvious) bug, and would quietly satisfy the check above if it were written as a comparison."""
        got = _render(_read(CSS))
        self.assertGreater(float(got["label"]["opacity"]), 0, got)
        self.assertGreater(float(got["replyCard"]["opacity"]), 0,
                           f"the '↩ replying to' label is visible and its post is not — this is "
                           f"exactly the reported screen: {got}")

    def test_this_check_would_have_caught_the_bug(self):
        """A test that cannot fail is not a test. Run the pre-fix rule and require the failure.

        Without this, rewording the pause rule to something that hides content in a NEW way would
        leave the assertions above passing for the wrong reason."""
        broken = _read(CSS).replace(
            "body.anim-off *, body.anim-off *::before, body.anim-off *::after{ animation: none !important; }",
            "body.anim-off *, body.anim-off *::before, body.anim-off *::after{ animation-play-state: paused !important; }")
        self.assertIn("animation-play-state: paused", broken,
                      "the rule this test is about moved — re-point it")
        got = _render(broken)
        self.assertEqual(got["replyCard"]["opacity"], "0",
                         "the old rule no longer hides a card, so this file is testing nothing")
        self.assertGreater(got["replyCard"]["height"], 0,
                           "the stranded card must keep its HEIGHT — that is why the ghost sweep's "
                           "zero-height probe could not see it")


class OneOwnerForTheClass(unittest.TestCase):
    """`anim-off` was armed and released from `visibilitychange` alone. On Android that event is late
    or coalesced away, and the resume path draws inside the gap."""

    def test_only_animoff_writes_the_class(self):
        writers = re.findall(r"classList\.(?:toggle|add|remove)\('anim-off'[^\n]*", APP)
        self.assertEqual(
            len(writers), 1,
            "anim-off is written in more than one place again — that is how it came to be armed "
            f"from two signals and released from one: {writers}")
        self.assertIn("function _animOff(on)", APP)

    def test_the_foreground_and_background_pair_own_it(self):
        for fn, want in (("_tlBackground", "_animOff(true)"), ("_tlForeground", "_animOff(false)")):
            body = re.search(r"function " + fn + r"\(\)\{(.*?)\n  \}", APP, re.S)
            self.assertTrue(body, f"{fn} moved — re-point this test")
            self.assertIn(want, body.group(1),
                          f"{fn} no longer owns anim-off; the class can drift from the app again")

    def test_the_background_half_sets_it_before_the_desktop_guard(self):
        """_tlBackground returns early on the desktop app (a covered window keeps streaming). The
        class must be set above that return or the desktop silently stops pausing its motion."""
        body = re.search(r"function _tlBackground\(\)\{(.*?)\n  \}", APP, re.S).group(1)
        self.assertLess(body.index("_animOff(true)"), body.index("_isDesktopApp()"), body)

    def test_the_native_resume_signal_reaches_it(self):
        """The gap that produced the bug: Capacitor's resume fires first, from native code that was
        never frozen. Both native foreground signals must go through _tlForeground."""
        for sig in (r"addListener\('resume',[^\n]*", r"if\(st && st\.isActive\)\{[^\n]*"):
            m = re.search(sig, APP)
            self.assertTrue(m, f"the native listener for {sig!r} moved — re-point this test")
            self.assertIn("_tlForeground()", m.group(0), m.group(0))

    def test_the_sweep_clears_a_stale_class_and_counts_it(self):
        """The backstop, and the measurement that would have named this in one round."""
        body = re.search(r"function _healGhostPairs\(box, measure\)\{(.*?)\n    const pairs =", APP, re.S)
        self.assertTrue(body, "_healGhostPairs moved — re-point this test")
        body = body.group(1)
        self.assertIn("classList.contains('anim-off')", body)
        self.assertIn("_ghosts.frozen++", body)
        # Before the `if(!pairs.length) return 0` — a fully-frozen timeline need not contain a reply.
        self.assertLess(APP.index("_ghosts.frozen++"), APP.index("if(!pairs.length) return 0;"),
                        "the stale-class check sits after the no-replies early return, so a frozen "
                        "timeline with no reply cards on it would never be healed")
        self.assertIn("frozen:0", APP, "PC.ghostStats must report it or the next report says nothing")


if __name__ == "__main__":
    unittest.main()
