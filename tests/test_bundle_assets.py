"""The bundled apps must carry every file the client asks for by URL.

Run: venv-unified/bin/python -m unittest tests.test_bundle_assets

The desktop app and the APK ship a copy of the client assembled by their own build-www.sh, and each
copies static/ by an explicit list of globs. A file the client references that no glob matches is
simply absent from the bundle — and the failure is silent in the worst way, because a 404 inside a
stylesheet produces no error anyone sees, just something missing on screen.

It has happened twice:

  * `client.css` @font-faces its two woff2 files at root-relative URLs, which the fetch shim never
    sees, so a bundle without them dropped the whole app to a system font.
  * `client.css` asks for `/static/os-wallpaper.webp`, and both scripts copied only `static/*.png` —
    so the bundled apps lost the desktop-mode wallpaper while the website kept it. Reported as
    "the windows app in desktop mode is missing the background, firefox has it".

Both were the same bug a second time, so this asserts the rule rather than the two files: whatever
the client references out of static/, both build scripts must copy.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = [os.path.join(ROOT, "desktop", "build-www.sh"),
           os.path.join(ROOT, "mobile", "build-www.sh")]
# Sources the client is assembled from. Anything here may name an asset by URL.
SOURCES = [os.path.join(ROOT, "static", "css", "client.css"),
           os.path.join(ROOT, "templates", "client.html")]
# Directories the scripts copy wholesale or by name — not the flat static/ files this guards.
HANDLED_PREFIXES = ("/static/js/", "/static/css/", "/static/vendor/", "/static/fonts/")


def referenced():
    """Every /static/<file> the client asks for, excluding directories copied by name."""
    out = set()
    for p in SOURCES:
        try:
            src = open(p, encoding="utf-8").read()
        except OSError:
            continue
        for m in re.finditer(r"/static/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", src):
            url = m.group(0)
            if url.startswith(HANDLED_PREFIXES):
                continue
            out.add(url)
    return out


class TestBundleCarriesWhatTheClientAsksFor(unittest.TestCase):
    def test_every_referenced_static_file_exists(self):
        """A reference to a file that is not in the repo is broken everywhere, bundle or not."""
        missing = [u for u in referenced()
                   if not os.path.exists(os.path.join(ROOT, u.lstrip("/")))]
        self.assertFalse(missing, "the client references files that do not exist: %s" % missing)

    def test_both_build_scripts_copy_every_referenced_type(self):
        """The actual guard. A type the client uses and a script does not copy is a file that exists
        on the website and is missing from the app."""
        exts = {os.path.splitext(u)[1].lower() for u in referenced()}
        self.assertTrue(exts, "found no static references at all — has the URL shape changed?")
        for script in SCRIPTS:
            body = open(script, encoding="utf-8").read()
            copied = set(re.findall(r"/static/\*(\.[A-Za-z0-9]+)", body))
            copied = {e.lower() for e in copied}
            for ext in sorted(exts):
                with self.subTest(script=os.path.basename(os.path.dirname(script)), ext=ext):
                    self.assertIn(
                        ext, copied,
                        "%s does not copy static/*%s, so every %s the client asks for is missing "
                        "from that bundle — a 404 inside a stylesheet, which shows up as something "
                        "absent on screen and nothing in any log."
                        % (script, ext, ext))

    def test_the_two_that_actually_broke(self):
        """Named, because each was found by a user rather than by a test."""
        refs = referenced()
        self.assertIn("/static/os-wallpaper.webp", refs,
                      "the desktop-mode wallpaper is no longer referenced — if it moved, this guard "
                      "should follow it rather than be deleted")
        for script in SCRIPTS:
            body = open(script, encoding="utf-8").read()
            self.assertIn("fonts/*.woff2", body,
                          "%s stopped copying the fonts; the app silently falls back to a system "
                          "font when they are absent" % script)


if __name__ == "__main__":
    unittest.main()
