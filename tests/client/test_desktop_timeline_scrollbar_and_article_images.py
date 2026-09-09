"""The desktop timeline has a real scrollbar, and an article's images have a bounded height.

Run: venv-unified/bin/python -m unittest \\
       tests.client.test_desktop_timeline_scrollbar_and_article_images
     (needs google-chrome-stable; it skips itself if there is no browser)

BOTH RULES ALREADY HAD A TEST AND NEITHER TEST COULD SEE THE THING IT WAS ABOUT. They read
client.css as TEXT and asserted that a selector and a declaration appear in it — which passes for a
rule that is overridden three lines later, for one whose media query does not match a desktop, and
for one whose selector no longer matches the markup the client builds. This RUNS the shipped
stylesheet against the shipped markup and measures:

  * a scrollbar is a scrollbar when it TAKES LAYOUT WIDTH — `offsetWidth - clientWidth` on the
    element that actually scrolls. The global reset is `*{scrollbar-width:none}`, deliberately, so
    every desktop surface is an override and an override is exactly what a text test cannot check.
  * an image is bounded when its rendered HEIGHT is smaller than the intrinsic one. The image here
    is 800x4000 — an ordinary phone photo dropped into a long-form post — and the failure it exists
    to prevent is that one picture becomes several screens of scrolling.

Chrome must be told it has a mouse: headless reports `hover:none, pointer:coarse`, so the
`(hover:hover) and (pointer:fine)` gate the classic-desktop rule sits behind does not match and the
measurement silently describes a phone. `--blink-settings=primaryHoverType=2,…` is what makes the
browser answer the question the stylesheet is asking.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = os.path.join(REPO, "static", "css", "client.css")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome") or shutil.which("chromium-browser"))

# 800x4000: portrait, far taller than any reading surface. Intrinsic size is the point.
TALL = ("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4"
        "MDAiIGhlaWdodD0iNDAwMCI+PHJlY3Qgd2lkdGg9IjgwMCIgaGVpZ2h0PSI0MDAwIiBmaWxsPSIjMDgwIi8+PC9zdmc+")

ARTICLE = ('<div class="article-view"><div class="markdown av-body">'
           '<img id="IMG" src="%s"></div></div>' % TALL)

# Two pages, because `body:not(.os-on)` and `body.os-on` are different desktops and one document
# cannot be both. Each carries a feed with more content than fits, which is the only state in which
# a scrollbar is a question at all.
CLASSIC = """<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="client.css">
<body><div class="app"><div class="main"><div class="feed" id="feed">
%s<div style="height:6000px"></div>
</div></div></div><pre id="out"></pre>
<script>%s</script>
""" % (ARTICLE.replace("IMG", "img"), "REPORT")

WINDOWED = """<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="client.css">
<body class="os-on"><div class="os-root"><div id="os-desk">
<div class="osw" style="left:10px;top:10px;width:700px;height:420px">
  <div class="osw-bar"><span class="osw-title">Social</span></div>
  <div class="osw-body"><div class="feed" id="feed">
  %s<div style="height:6000px"></div>
  </div></div>
</div></div></div><pre id="out"></pre>
<script>%s</script>
""" % (ARTICLE.replace("IMG", "img"), "REPORT")

REPORT = """
setTimeout(() => {
  const feed = document.getElementById('feed'), img = document.getElementById('img');
  document.getElementById('out').textContent = JSON.stringify({
    gutter: feed.offsetWidth - feed.clientWidth,
    scrolls: feed.scrollHeight > feed.clientHeight,
    imgH: img.offsetHeight,
    naturalH: img.naturalHeight,
    zoom: parseFloat(getComputedStyle(document.body).zoom) || 1,
    hover: matchMedia('(hover:hover) and (pointer:fine)').matches,
  });
}, 500);
"""


def _render(page, css_text, width=1400, height=900, mouse=True):
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "client.css"), "w", encoding="utf-8") as fh:
            fh.write(css_text)
        html = os.path.join(tmp, "p.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(page.replace("REPORT", REPORT))
        cmd = [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
               "--virtual-time-budget=2500", "--window-size=%d,%d" % (width, height),
               "--user-data-dir=" + os.path.join(tmp, "profile")]
        if mouse:
            # Headless says "no mouse", and every fine-pointer rule in the stylesheet then sits
            # behind a media query that cannot match — a measurement of a phone, reported as a
            # desktop. Answer the question the stylesheet actually asks.
            cmd.append("--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
                       "primaryPointerType=4,availablePointerTypes=4")
        cmd += ["--dump-dom", "file://" + html]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        assert res.returncode == 0, res.stderr[-2000:]
        m = re.search(r'<pre id="out">(\{.*?\})</pre>', res.stdout, re.S)
        assert m, "the page never reported — see the dump:\n" + res.stdout[:2000]
        return json.loads(m.group(1))


def _css():
    with open(CSS, encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(CHROME, "needs Chrome to resolve real CSS")
class TheTimelineScrolls(unittest.TestCase):
    def test_the_classic_desktop_timeline_reserves_a_scrollbar(self):
        got = _render(CLASSIC, _css())
        self.assertTrue(got["hover"], "the browser did not report a mouse — the fine-pointer rules "
                                      "were never evaluated and this measurement means nothing")
        self.assertTrue(got["scrolls"], got)
        self.assertGreaterEqual(
            got["gutter"], 8,
            "the timeline reserves %spx for a scrollbar at 1400x900 with a mouse: the global "
            "`*{scrollbar-width:none}` reset is still winning, so there is nothing to grab and "
            "nothing showing how far down the feed you are (%s)" % (got["gutter"], got))

    def test_a_desktop_window_timeline_reserves_one_too(self):
        """The windowed desktop is the OTHER desktop, with its own selector — and the one people
        described, since `body.os-on` is what a PosterChanOS Social window is."""
        got = _render(WINDOWED, _css())
        self.assertTrue(got["scrolls"], got)
        self.assertGreaterEqual(got["gutter"], 8, got)

    def test_a_phone_keeps_its_barless_feed(self):
        """The reset is right on touch: a permanent 10px gutter on a 390px screen is 2.5% of the
        width spent on something a finger never uses."""
        got = _render(CLASSIC, _css(), width=390, height=780, mouse=False)
        self.assertTrue(got["scrolls"], got)
        self.assertEqual(got["gutter"], 0, "a phone feed reserved scrollbar width: %s" % (got,))

    def test_this_check_would_have_caught_a_missing_override(self):
        """A test that cannot fail is not a test. Delete the desktop override and require the
        global reset to win, which is the state this rule exists to correct."""
        broken = re.sub(r"\n[^\n]*\.feed\{scrollbar-width:thin[^\n]*", "\n", _css())
        self.assertNotEqual(broken, _css(), "the override moved — re-point this test")
        got = _render(CLASSIC, broken)
        self.assertEqual(got["gutter"], 0,
                         "removing the override no longer removes the scrollbar, so this file is "
                         "measuring something else: %s" % (got,))


@unittest.skipUnless(CHROME, "needs Chrome to resolve real CSS")
class AnArticleImageIsBounded(unittest.TestCase):
    """A 4000px phone photo in a long-form post must not become several screens of scrolling. The
    original stays reachable through the lightbox; the reading surface is what is being bounded."""

    def test_the_reading_surface_bounds_a_very_tall_image(self):
        got = _render(CLASSIC, _css())
        cap = min(0.60 * got["naturalH"], 560 / got["zoom"])   # the rule, in the page's own pixels
        self.assertLess(got["imgH"], got["naturalH"], got)
        self.assertLessEqual(got["imgH"], round(560 / got["zoom"]) + 1,
                             "an article image rendered %spx tall — the 560px ceiling is not "
                             "applying (%s)" % (got["imgH"], got))
        self.assertGreater(got["imgH"], 100, "bounded to nothing is not bounded (%s)" % (got,))
        self.assertGreater(cap, 0)

    def test_it_is_bounded_in_a_desktop_window_as_well(self):
        got = _render(WINDOWED, _css())
        self.assertLess(got["imgH"], got["naturalH"], got)
        self.assertLessEqual(got["imgH"], round(560 / got["zoom"]) + 1, got)

    def test_this_check_would_have_caught_an_unbounded_image(self):
        """Run the pre-rule stylesheet: with only `.markdown img{max-width:100%}` the picture keeps
        its aspect ratio at the column's width, which for an 800x4000 photo is thousands of pixels
        of one image."""
        broken = re.sub(r"\n\.article-view \.av-body img\{[^}]*\}", "\n", _css())
        self.assertNotEqual(broken, _css(), "the article image rule moved — re-point this test")
        got = _render(CLASSIC, broken)
        self.assertGreater(got["imgH"], 1000,
                           "without the rule the image is no longer unbounded, so this file is "
                           "measuring something else: %s" % (got,))
