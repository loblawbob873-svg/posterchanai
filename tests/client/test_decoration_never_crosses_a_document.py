"""The flagship theme's decoration may paint the app. It may never paint the thing you are READING.

Run: venv-unified/bin/python -m unittest tests.client.test_decoration_never_crosses_a_document
     (the browser half needs google-chrome-stable; it skips itself if there is no browser)

THE BUG THIS EXISTS FOR — "office documents should not have that cyberpunk effect ! same for
emails, ... same for viewing media".

The cyberpunk theme (the bare `:root`; every `[data-theme]` hides all three) puts three full-viewport
layers under `<body>`:

    .scanlines   z-index 9990   a CRT sheet, ON TOP of the entire page
    .grid-bg     z-index -1     a neon grid, BEHIND content
    .city-bg     z-index -2     the parallax skyline, BEHIND content

Only ONE of them was ever the complaint, and this file is written to say which. Measured against the
shipped stylesheet, over the real Office editor frame, the real HTML-mail frame and the real Preview
media surface, `.scanlines` re-tints 38% of every white document pixel (#ffffff -> #e9e9e9) — a
spreadsheet, an email and a photo all read through a CRT grille. The two underlays sit behind an
opaque surface and change ZERO pixels, so they are not touched: hiding a layer that was already
invisible would be a change nobody can see, in the one theme whose character it is.

The client already knew this, in one place only. `.pc-document-focus .scanlines{display:none}` is set
by the windowed desktop's window manager when an `osw-document` window (Preview / Office / Email)
takes focus. Everywhere there is no window manager it was never set at all:

  * classic web and the APK, where Office is a whole view and Preview is a full-screen sheet
  * a POPPED-OUT window (`html.pc-oswin`), which returns early from `PCOS.enter()` and runs no
    desktop — and which is exactly where the owner reads Office and Preview on PosterChanOS

So the fix is not "hide the scanlines on three more screens". It is a RULE, and the rule is that a
surface showing a document, a message or a piece of media marks itself `pc-doc`, after which one
stylesheet rule turns the page's decoration off for as long as such a surface is in the DOM:

    body:not(.os-on):has(.pc-doc) .scanlines{display:none!important}

Two properties worth keeping, both asserted below:

  no state      the marker is on the SURFACE, so it cannot outlive the surface. `pc-document-focus`
                is a flag on <html> and had to be taught to clear itself twice (closeWin, then
                minimise) before it stopped leaking onto a bare desktop.
  one owner     `body.os-on` is excluded deliberately. On the desktop a document owns a WINDOW, not
                the page, and the window manager already answers this better — by FOCUS — so a
                Preview parked behind the timeline must not strip the whole desktop's decoration.

The measurement is PIXELS, not the presence of a selector: a rule that is present and out-scoped,
or a marker that stopped reaching the surface, both leave the stylesheet looking correct and the
document looking wrong. `test_this_check_would_have_caught_the_bug` re-runs the pre-fix stylesheet
and requires the tint back, so the check cannot pass by measuring nothing.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = os.path.join(REPO, "static", "css", "client.css")
APPJS = os.path.join(REPO, "static", "js", "client", "app.js")
PREVIEWJS = os.path.join(REPO, "static", "js", "client", "preview.js")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

try:
    from PIL import Image
except Exception:                                     # pragma: no cover - Pillow is a hard dep
    Image = None


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# The rule as it ships. Kept as a literal so a reword has to come through this file.
RULE = "body:not(.os-on):has(.pc-doc) .scanlines{display:none!important}"

# The three decorative layers, exactly as templates/client.html writes them into <body>.
OVERLAYS = {
    "scanlines": '<div class="scanlines" aria-hidden="true"></div>',
    "grid-bg": '<div class="grid-bg" aria-hidden="true"></div>',
    "city-bg": '<div class="city-bg" aria-hidden="true"><span class="city-far"></span>'
               '<span class="city-mid"></span><span class="city-near"></span></div>',
}

# The three readers, built from the shipped class names and nesting. Each is a white page of the
# kind the owner reported: a Collabora frame, an HTML email, a picture. `{doc}` is where the marker
# goes, so one page source can render both the fixed and the pre-fix shape.
SURFACES = {
    "office": '<div id="app"><div class="main"><div class="feed feed-office">'
              '<div class="office-view" style="height:500px">'
              '<div class="office-frame {doc}" style="height:460px;background:#fff"></div>'
              "</div></div></div></div>",
    "mail": '<div id="app"><div class="main"><div class="feed feed-dm">'
            '<div class="mail-root {doc}"><div class="mail-wrap">'
            '<div class="mail-side"></div><div class="mail-list"></div>'
            '<div class="mail-read has-open"><div class="mail-thread"><div class="mail-msg open">'
            '<div class="mail-body"><div class="mail-html" style="height:300px;background:#fff"></div>'
            "</div></div></div></div></div></div></div></div>",
    "preview": '<div class="pv-sheet pv-host {doc}"><div class="pv-bar"></div>'
               '<div class="pv-body pv-img-wrap">'
               '<div class="pv-img" style="width:300px;height:220px;background:#fff"></div></div></div>',
}


def _shoot(tmp, css_text, body, tag, html_class="", body_class=""):
    """Screenshot one page against a real stylesheet and hand back the bitmap."""
    css = os.path.join(tmp, "client.css")
    if not os.path.exists(css):
        with open(css, "w", encoding="utf-8") as fh:
            fh.write(css_text)
    page = os.path.join(tmp, tag + ".html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="client.css">'
                 "<style>html,body{margin:0;height:100%}</style>"
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


def _tint(clean, decorated):
    """How much of the white DOCUMENT the decoration repainted.

    Counts only pixels the reader itself drew white, so nothing here can be satisfied by the page
    background: (pixels sampled, pixels the overlays changed)."""
    total = changed = 0
    for y in range(clean.size[1]):
        for x in range(0, clean.size[0], 3):
            px = clean.getpixel((x, y))
            if px == (255, 255, 255):
                total += 1
                if decorated.getpixel((x, y)) != px:
                    changed += 1
    return total, changed


@unittest.skipUnless(CHROME and Image, "needs Chrome + Pillow to measure real pixels")
class DecorationNeverCrossesADocument(unittest.TestCase):

    def _measure(self, css_text, name, layers, doc=True, html_class=""):
        body = SURFACES[name].format(doc="pc-doc" if doc else "")
        with tempfile.TemporaryDirectory() as tmp:
            clean = _shoot(tmp, css_text, body, name + "_clean", html_class)
            painted = _shoot(tmp, css_text, "".join(OVERLAYS[k] for k in layers) + body,
                             name + "_painted", html_class)
        total, changed = _tint(clean, painted)
        self.assertGreater(total, 500,
                           "%s rendered no white document to measure — the harness is broken, "
                           "not the stylesheet" % name)
        return total, changed

    def test_no_decoration_reaches_an_office_document_an_email_or_a_photo(self):
        """The regression test, in pixels, for all three surfaces the owner named."""
        css = _read(CSS)
        for name in ("office", "mail", "preview"):
            total, changed = self._measure(css, name, OVERLAYS)
            self.assertEqual(
                changed, 0,
                "%d of %d pixels of the %s document are repainted by the theme's decoration. "
                "A document must look like the document: nothing decorative may be drawn over "
                "(or, through a transparent surface, behind) what the user is reading."
                % (changed, total, name))

    def test_a_popped_out_window_is_covered_too(self):
        """`html.pc-oswin` runs no desktop — it is one view with nothing behind it, and it is where
        Office and Preview are actually read on PosterChanOS. It sets no `os-on`, so the rule must
        reach it; measured rather than reasoned, because the exclusion is written as `:not(.os-on)`
        and a window that DID set that class would silently keep the scanlines."""
        css = _read(CSS)
        for name in ("office", "preview"):
            total, changed = self._measure(css, name, OVERLAYS, html_class="pc-oswin")
            self.assertEqual(changed, 0,
                             "%d/%d pixels of %s are decorated inside a popped-out window" %
                             (changed, total, name))

    def test_this_check_would_have_caught_the_bug(self):
        """A test that cannot fail is not a test. Run the pre-fix stylesheet and require the tint.

        Both halves matter. Removing the RULE must bring the scanlines back — that is the bug. And
        removing the MARKER must bring them back too, which is what stops the rule from silently
        becoming decoration itself if `pc-doc` ever stops reaching a surface."""
        css = _read(CSS)
        self.assertIn(RULE, css, "the rule was reworded — re-point this test at its new form")
        pre_fix = css.replace(RULE, "")
        for name in ("office", "mail", "preview"):
            total, changed = self._measure(pre_fix, name, OVERLAYS)
            self.assertGreater(
                changed, total // 5,
                "without the rule the %s document is NOT re-tinted, so this file is measuring "
                "nothing (%d/%d)" % (name, changed, total))
            _, unmarked = self._measure(css, name, OVERLAYS, doc=False)
            self.assertGreater(
                unmarked, total // 5,
                "a surface with no `pc-doc` marker is left undecorated anyway — the rule is not "
                "what is doing the work here (%s: %d/%d)" % (name, unmarked, total))

    def test_only_the_scanlines_were_ever_guilty(self):
        """WHICH layer, measured. The two underlays are behind an opaque surface and change nothing,
        which is why the fix leaves them alone — and why this asserts they were innocent rather than
        assuming it. If a reader ever becomes transparent this test fails and says so."""
        pre_fix = _read(CSS).replace(RULE, "")
        for name in ("office", "mail", "preview"):
            total, over = self._measure(pre_fix, name, ("scanlines",))
            self.assertGreater(over, 0, "%s: .scanlines no longer paints over a document, so the "
                                        "bug being fixed here has moved" % name)
            _, under = self._measure(pre_fix, name, ("grid-bg", "city-bg"))
            self.assertEqual(under, 0,
                             "%d/%d pixels of the %s document now show the grid/city UNDERLAY "
                             "through it. That is a transparent reading surface, not a decoration "
                             "problem: make the surface opaque rather than hiding the layer."
                             % (under, total, name))

    def test_the_desktop_still_decides_for_itself(self):
        """`body.os-on` is excluded on purpose: there a document owns a window, not the page, and
        `pc-document-focus` follows FOCUS. A parked Preview must not strip the desktop."""
        css = _read(CSS)
        body = SURFACES["preview"].format(doc="pc-doc")
        with tempfile.TemporaryDirectory() as tmp:
            page = '<div class="os-root"></div>' + body
            clean = _shoot(tmp, css, page, "desk_clean", body_class="os-on")
            painted = _shoot(tmp, css, OVERLAYS["scanlines"] + page, "desk_painted",
                             body_class="os-on")
        total, changed = _tint(clean, painted)
        self.assertGreater(
            changed, 0,
            "a `pc-doc` surface now suppresses the scanlines on the windowed desktop as well. "
            "That is a second owner for one decision: os.js already toggles `pc-document-focus` "
            "from focusWin/minimise/closeWin, and an open-but-unfocused document window would now "
            "hold the whole desktop undecorated (%d/%d)" % (changed, total))


class TheMarkerReachesEveryReader(unittest.TestCase):
    """The rule is only worth as much as the marker. These are the three construction points, each
    chosen because it is the ONLY one for its reader — an office editor built for a desktop window,
    a classic view and a modal all come from one `bodyHTML`; both Preview hosts go through one
    `mount()`. A fourth reader is expected to add itself here."""

    def test_office_marks_its_editor_once_for_all_three_mounts(self):
        app = _read(APPJS)
        self.assertIn('<iframe class="office-frame pc-doc"', app,
                      "the office editor frame lost its `pc-doc` marker — the desktop window, the "
                      "classic view and the modal all build from this one string, so all three go "
                      "back to reading through the scanlines at once")
        self.assertEqual(app.count('class="office-frame'), 1,
                         "a second office frame was written; both need the marker or neither does")

    def test_email_marks_its_reader(self):
        app = _read(APPJS)
        self.assertIn('class="mail-root pc-doc"', app,
                      "the Email reader lost its `pc-doc` marker")

    def test_preview_marks_both_of_its_hosts_from_one_place(self):
        """A desktop window's slot and the full-screen sheet are different elements; `mount()` is
        the one function both of them are handed to, which is why the marker is set there."""
        js = _read(PREVIEWJS)
        m = re.search(r"function mount\(host[^)]*\)\s*\{(.*?)host\.innerHTML", js, re.S)
        self.assertTrue(m, "preview.js mount() moved — re-point this test")
        self.assertIn("classList.add('pc-doc')", m.group(1),
                      "Preview no longer marks its host as a document surface, so the media viewer "
                      "reads through the scanlines again on every non-desktop client")
        self.assertEqual(js.count("classList.add('pc-doc')"), 1,
                         "the marker is set in more than one place in preview.js — one owner")


class TheStylesheetKeepsItsShape(unittest.TestCase):
    def test_the_rule_is_scoped_off_the_desktop_and_keyed_on_the_marker(self):
        css = _read(CSS)
        self.assertIn(RULE, css)
        # The desktop's own, focus-aware suppression must survive beside it.
        self.assertIn(".pc-document-focus .scanlines{display:none!important}", css,
                      "the window manager's focus-aware suppression was removed; the new rule "
                      "deliberately does NOT cover body.os-on, so the desktop would lose both")

    def test_the_rule_names_no_screen(self):
        """It is a rule, not a list. If Office/Email/Preview appear in its selector, the next
        reader that is added silently does not get it."""
        line = [l for l in _read(CSS).splitlines() if RULE in l]
        self.assertEqual(len(line), 1, line)
        for word in ("office", "mail", "pv-", "preview"):
            self.assertNotIn(word, line[0],
                             "the suppression selector names a specific screen (%r): make the "
                             "surface wear `pc-doc` instead" % word)


if __name__ == "__main__":
    unittest.main()
