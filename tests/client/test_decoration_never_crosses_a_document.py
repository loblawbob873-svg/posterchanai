"""The flagship theme's decoration may paint the app. It may never paint the thing you are LOOKING AT.

Run: venv-unified/bin/python -m unittest tests.client.test_decoration_never_crosses_a_document
     (the browser half needs google-chrome-stable; it skips itself if there is no browser)

THE BUGS THIS EXISTS FOR, reported in two rounds and fixed as one rule:

  "office documents should not have that cyberpunk effect ! same for emails, ... same for viewing
   media"
  "you need to stop doing that cyberpunk effect on background images, looks liney" / "same for post
   images" / "ruins the user experience"

The cyberpunk theme (the bare `:root`; every `[data-theme]` hides all three) puts three
full-viewport layers under `<body>`:

    .scanlines   z-index -1     a CRT sheet          — was 9990, i.e. ON TOP of the entire page
    .grid-bg     z-index -1     a neon grid
    .city-bg     z-index -2     the parallax skyline

Only ONE of them was ever the complaint, and this file is written to say which, in pixels. Against
the stylesheet as it shipped BEFORE the fix, `.scanlines` repainted:

    38% of a white Office page          34% of a photo in the timeline
    38% of an HTML email                36% of the desktop wallpaper
    38% of a Preview media surface

— every third row of each, which is exactly what "liney" describes. The two underlays sit behind an
opaque surface and change ZERO pixels, so they are untouched: hiding a layer that was already
invisible would be a change nobody can see, in the one theme whose character it is.

THE FIX IS THE STACKING ORDER, NOT A LIST OF SCREENS. The sheet is an underlay now, so the rule is
structural: **the theme's decoration is drawn UNDER the app, never over it.** Everything the user
came to read or look at is painted after it and is therefore clean by construction — including the
next reader nobody has written yet, and including somebody's own wallpaper, which no marker-based
rule would ever have covered because a wallpaper is not a document.

Two mechanisms were deleted by this, and both are worth remembering as shapes:

  `.pc-document-focus .scanlines`        driven by the window manager's FOCUS, and patched twice
                                        (closeWin, then minimise) because the class outlived the
                                        window that set it — a flag on <html> describing something
                                        that had already closed.
  `body:not(.os-on):has(.pc-doc) …`      driven by a marker three surfaces had to remember to wear,
                                        which covered exactly the surfaces somebody had thought of.

Both were approximations of "don't paint over what I am looking at". Neither could have caught a
photo in the timeline, because nobody would mark a timeline as a document.

The measurement is PIXELS, not the presence of a selector: a rule that is present and out-scoped, or
a marker that stopped reaching a surface, both leave the stylesheet looking correct and the picture
looking wrong. `test_this_check_would_have_caught_the_bug` re-runs the pre-fix stylesheet and
requires the tint back on all five surfaces, so the check cannot pass by measuring nothing — and
`test_the_theme_still_has_its_character` requires the layer to be VISIBLE on an empty page, so it
cannot be "fixed" by quietly deleting the flagship's decoration.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = os.path.join(REPO, "static", "css", "client.css")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

try:
    from PIL import Image
except Exception:                                     # pragma: no cover - Pillow is a hard dep
    Image = None


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# The rule as it ships, kept as a literal so a change has to come through this file. The z-index is
# the whole fix: at 9990 this layer is drawn over everything on the page.
RULE = ".scanlines{position:fixed;inset:0;pointer-events:none;z-index:-1;"
PRE_FIX = ".scanlines{position:fixed;inset:0;pointer-events:none;z-index:9990;"

# The three decorative layers, exactly as templates/client.html writes them into <body>.
OVERLAYS = {
    "scanlines": '<div class="scanlines" aria-hidden="true"></div>',
    "grid-bg": '<div class="grid-bg" aria-hidden="true"></div>',
    "city-bg": '<div class="city-bg" aria-hidden="true"><span class="city-far"></span>'
               '<span class="city-mid"></span><span class="city-near"></span></div>',
}

# A 1x1 white picture, used wherever a surface needs real image bytes.
PHOTO = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='8' height='8'>"
         "<rect width='8' height='8' fill='white'/></svg>")

# The five surfaces, built from the shipped class names and nesting: the three readers the first
# report named, and the two pictures the second one did.
SURFACES = {
    "office": '<div id="app"><div class="main"><div class="feed feed-office">'
              '<div class="office-view" style="height:500px">'
              '<div class="office-frame" style="height:460px;background:#fff"></div>'
              "</div></div></div></div>",
    "mail": '<div id="app"><div class="main"><div class="feed feed-dm">'
            '<div class="mail-root"><div class="mail-wrap">'
            '<div class="mail-side"></div><div class="mail-list"></div>'
            '<div class="mail-read has-open"><div class="mail-thread"><div class="mail-msg open">'
            '<div class="mail-body"><div class="mail-html" style="height:300px;background:#fff"></div>'
            "</div></div></div></div></div></div></div></div>",
    "preview": '<div class="pv-sheet pv-host"><div class="pv-bar"></div>'
               '<div class="pv-body pv-img-wrap">'
               '<div class="pv-img" style="width:300px;height:220px;background:#fff"></div></div></div>',
    # A photo in the timeline: the surface NO marker-based rule was ever going to cover.
    "timeline_photo": '<div id="app"><div class="main"><div class="feed">'
                      '<div class="note"><div class="body"><div class="txt">hello</div>'
                      '<div class="media"><img src="%s" style="width:420px;height:300px;display:block">'
                      "</div></div></div></div></div></div>" % PHOTO,
    # The desktop wallpaper: the user's own picture from the drive's Backgrounds folder, under the
    # scrim that keeps icon labels readable (so its colour is NOT white — see _tint's `colour`).
    "wallpaper": '<div class="os-root"><div class="os-desk has-bg" style="height:600px;'
                 "background-image:url('%s')\"></div></div>" % PHOTO,
}

# The desktop is a body class, not a wrapper.
BODY_CLASS = {"wallpaper": "os-on"}

# Where to sample a surface's own colour, for the one surface that is not white.
SAMPLE = {"wallpaper": (450, 350)}


def _shoot(tmp, css_text, body, tag, html_class="", body_class=""):
    """Screenshot one page against a real stylesheet and hand back the bitmap."""
    css = os.path.join(tmp, "client.css")
    if not os.path.exists(css):
        with open(css, "w", encoding="utf-8") as fh:
            fh.write(css_text)
    page = os.path.join(tmp, tag + ".html")
    with open(page, "w", encoding="utf-8") as fh:
        # ANIMATIONS OFF, and this is not cosmetic. `.note` carries `animation: noteRise .3s both`,
        # which STARTS at opacity:0 — so a card screenshotted at two different moments is two
        # different pictures, and the diff reports every pixel as changed no matter what the
        # decoration did. Measured: this file passed alone and failed with the full suite running,
        # on the one surface built from a real timeline card. Nothing here is asking about
        # animation; it is asking which layer paints over which, and a still frame answers that
        # exactly. (This is the same class of bug as `anim-off` freezing cards at opacity 0 — see
        # tests/client/test_anim_off_never_hides_content.py.)
        fh.write('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="client.css">'
                 "<style>html,body{margin:0;height:100%}"
                 "*,*::before,*::after{animation:none!important;transition:none!important}</style>"
                 '<html class="' + html_class + '"><body class="' + body_class + '">'
                 + body + "</body>")
    png = os.path.join(tmp, tag + ".png")
    res = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--window-size=900,700", "--virtual-time-budget=1500",
         "--user-data-dir=" + os.path.join(tmp, "p_" + tag), "--screenshot=" + png,
         "file://" + page],
        capture_output=True, text=True, timeout=120)
    assert os.path.exists(png), "chrome produced no screenshot: " + res.stderr[-2000:]
    return Image.open(png).convert("RGB")


def _tint(clean, decorated, colour=(255, 255, 255)):
    """How much of the SURFACE the decoration repainted.

    Counts only pixels the surface itself drew in its own colour, so nothing here can be satisfied
    by the page backdrop — which the decoration is entitled to paint. (sampled, changed)."""
    total = changed = 0
    for y in range(clean.size[1]):
        for x in range(0, clean.size[0], 3):
            px = clean.getpixel((x, y))
            if px == colour:
                total += 1
                if decorated.getpixel((x, y)) != px:
                    changed += 1
    return total, changed


@unittest.skipUnless(CHROME and Image, "needs Chrome + Pillow to measure real pixels")
class DecorationNeverCrossesADocument(unittest.TestCase):

    def _measure(self, css_text, name, layers, html_class=""):
        body = SURFACES[name]
        body_class = BODY_CLASS.get(name, "")
        with tempfile.TemporaryDirectory() as tmp:
            clean = _shoot(tmp, css_text, body, name + "_clean", html_class, body_class)
            painted = _shoot(tmp, css_text, "".join(OVERLAYS[k] for k in layers) + body,
                             name + "_painted", html_class, body_class)
        colour = clean.getpixel(SAMPLE[name]) if name in SAMPLE else (255, 255, 255)
        total, changed = _tint(clean, painted, colour)
        self.assertGreater(total, 500,
                           "%s rendered no surface to measure — the harness is broken, not the "
                           "stylesheet" % name)
        return total, changed

    def test_no_decoration_reaches_a_document_an_email_a_photo_or_a_wallpaper(self):
        """The regression test, in pixels, for every surface both reports named."""
        css = _read(CSS)
        for name in SURFACES:
            total, changed = self._measure(css, name, OVERLAYS)
            self.assertEqual(
                changed, 0,
                "%d of %d pixels of the %s are repainted by the theme's decoration. Nothing "
                "decorative may be drawn over (or, through a transparent surface, behind) what the "
                "user came to read or look at." % (changed, total, name))

    def test_a_popped_out_window_is_covered_too(self):
        """`html.pc-oswin` runs no desktop — it is one view with nothing behind it, and it is where
        Office and Preview are actually read on PosterChanOS. Measured rather than reasoned: the
        previous fix was written as `:not(.os-on)` and a window that DID set that class kept the
        scanlines, which is the kind of thing a stacking-order rule cannot get wrong but a
        selector-scoped one can."""
        css = _read(CSS)
        for name in ("office", "preview", "timeline_photo"):
            total, changed = self._measure(css, name, OVERLAYS, html_class="pc-oswin")
            self.assertEqual(changed, 0,
                             "%d/%d pixels of %s are decorated inside a popped-out window" %
                             (changed, total, name))

    def test_this_check_would_have_caught_the_bug(self):
        """A test that cannot fail is not a test. Run the pre-fix stylesheet and require the tint."""
        css = _read(CSS)
        self.assertIn(RULE, css, "the `.scanlines` rule was reworded — re-point this test at it, and "
                                 "check the z-index is still negative while you are there")
        pre_fix = css.replace(RULE, PRE_FIX)
        for name in SURFACES:
            total, changed = self._measure(pre_fix, name, OVERLAYS)
            self.assertGreater(
                changed, total // 5,
                "with the scanline sheet back on top the %s is NOT re-tinted, so this file is "
                "measuring nothing (%d/%d)" % (name, changed, total))

    def test_only_the_scanlines_were_ever_guilty(self):
        """WHICH layer, measured. The two underlays change nothing, which is why the fix leaves them
        alone — and why this asserts they were innocent rather than assuming it. If a reading surface
        ever becomes transparent this test fails and says so."""
        pre_fix = _read(CSS).replace(RULE, PRE_FIX)
        for name in SURFACES:
            total, over = self._measure(pre_fix, name, ("scanlines",))
            self.assertGreater(over, 0, "%s: .scanlines no longer paints over it, so the bug being "
                                        "fixed here has moved" % name)
            _, under = self._measure(pre_fix, name, ("grid-bg", "city-bg"))
            self.assertEqual(under, 0,
                             "%d/%d pixels of the %s now show the grid/city UNDERLAY through it. "
                             "That is a transparent surface, not a decoration problem: make the "
                             "surface opaque rather than hiding the layer." % (under, total, name))

    def test_the_theme_still_has_its_character(self):
        """The other half of the fix, and the one a careless "fix" would lose: moving the sheet
        BEHIND the app must not be the same thing as deleting it. On an empty app shell — the page's
        own backdrop, which is what a CRT texture was always for — it must still paint."""
        css = _read(CSS)
        shell = '<div id="app"><div class="side"></div><div class="main"><div class="feed"></div></div></div>'
        with tempfile.TemporaryDirectory() as tmp:
            clean = _shoot(tmp, css, shell, "shell_clean")
            painted = _shoot(tmp, css, OVERLAYS["scanlines"] + shell, "shell_painted")
        changed = sum(1 for y in range(clean.size[1]) for x in range(0, clean.size[0], 3)
                      if clean.getpixel((x, y)) != painted.getpixel((x, y)))
        self.assertGreater(changed, 10000,
                           "the scanline layer no longer paints anything at all (%d pixels). The "
                           "cyberpunk theme is the flagship and this is its character — it belongs "
                           "under the app, not gone." % changed)


class TheDeletedMechanismsStayDeleted(unittest.TestCase):
    """Both were per-surface approximations of a stacking-order rule. Re-adding one is how this
    drifts back to "hide the scanlines on three more screens", which is what left a photo in the
    timeline and somebody's wallpaper reading through a CRT grille for as long as they did."""

    # `not in`, not assertNotIn: the container here is the whole stylesheet, and a failure that
    # prints it buries its own message under 400KB. The needles carry `{display:none` so this asks
    # about a RULE and not about the comment above them that explains why there isn't one.
    def test_no_focus_tracked_class_came_back(self):
        css = _read(CSS)
        self.assertTrue(".pc-document-focus .scanlines{display:none" not in css,
                        "the focus-tracked suppression rule is back in client.css")
        osjs = _read(os.path.join(REPO, "static", "js", "client", "os.js"))
        self.assertTrue("classList.toggle('pc-document-focus'" not in osjs
                        and "classList.add('pc-document-focus'" not in osjs,
                        "os.js is tracking which window has focus in order to hide decoration "
                        "again — the underlay makes that unnecessary, and the class outliving its "
                        "window is a bug this already had twice")

    def test_no_per_surface_marker_came_back(self):
        css = _read(CSS)
        self.assertTrue(":has(.pc-doc) .scanlines{display:none" not in css,
                        "a marker-based suppression rule is back. It can only ever cover the "
                        "surfaces somebody thought to mark; the stacking order covers the rest.")


if __name__ == "__main__":
    unittest.main()
