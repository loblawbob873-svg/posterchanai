"""ONE ICON SET FOR TWO RENDERERS, and the check that stops it becoming two.

The native screens (launcher, dialer, SMS) are drawn by Android and cannot use
static/js/client/sprite.js — so scripts/gen_android_icons.py transcribes the symbols they need into
VectorDrawables, and generates the sprite-name to R.drawable switch beside them.

This is the drift guard. An icon redrawn in the sprite, or a symbol renamed, or a hand-edit to one of
the generated files, all fail here — where the alternative is the phone's Messages screen and the
app's Messages screen slowly becoming different products.
"""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(ROOT, "scripts", "gen_android_icons.py")
DRAWABLE = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res", "drawable")
TILES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                     "place", "poster", "app", "home", "HomeTiles.java")


@unittest.skipIf(not os.path.isdir(DRAWABLE), "no android sources here")
class IconSprite(unittest.TestCase):

    def test_the_checked_in_icons_match_the_sprite(self):
        r = subprocess.run([sys.executable, GEN, "--check"],
                           capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_nothing_bakes_a_colour_into_an_icon(self):
        """Nine themes cost nine tints, not nine icon sets. A path that carries a real colour cannot
        be tinted at runtime, so it would stay one theme's colour on every other theme."""
        for f in os.listdir(DRAWABLE):
            if not f.startswith("ic_pc_"):
                continue
            src = open(os.path.join(DRAWABLE, f), encoding="utf-8").read()
            for m in re.finditer(r'android:(?:stroke|fill)Color="(#[0-9A-Fa-f]+)"', src):
                self.assertIn(m.group(1).upper(), ("#FFFFFFFF", "#00000000"),
                              f + " bakes " + m.group(1))

    def test_every_tile_in_the_catalogue_has_an_icon(self):
        """A tile whose icon does not resolve draws NOTHING — a blank square with a label under it,
        which reads as a broken app rather than as a missing file."""
        src = open(TILES, encoding="utf-8").read()
        wanted = set(re.findall(r'new Tile\("[^"]*",\s*"[^"]*",\s*"([a-z0-9-]+)"', src))
        self.assertTrue(wanted, "could not read the tile catalogue")
        for icon in sorted(wanted):
            path = os.path.join(DRAWABLE, "ic_pc_%s.xml" % icon.replace("-", "_"))
            self.assertTrue(os.path.exists(path),
                            "HomeTiles asks for '%s'; add it to WANTED in scripts/gen_android_icons.py"
                            % icon)

    def test_no_emoji_anywhere_in_the_native_screens(self):
        """FLAT ICONS, CONSISTENTLY — the same rule the web client's action verbs were converted to.

        Emoji are the wrong weight beside UI type, render differently on every platform, and cannot
        inherit a colour or a state, so a screen mixing them with the sprite reads as unstyled."""
        java = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                            "place", "poster", "app")
        strings = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res",
                               "values", "strings.xml")
        targets = []
        for pkg in ("home", "sms", "phone", "ui"):
            d = os.path.join(java, pkg)
            if os.path.isdir(d):
                targets += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".java")]
        targets.append(strings)
        # The emoji planes, plus the dingbats/arrows/misc-symbols blocks that carry the glyphs a
        # button is most often labelled with by accident: ▶ ‖ ☎ ✕ →.
        bad = re.compile("[\U0001F000-\U0001FAFF\u2190-\u27BF\u2B00-\u2BFF\u2600-\u26FF"
                         "\u2000-\u2BFF\uFE0F]")
        # STRING LITERALS AND RESOURCES ONLY. Prose may say whatever it likes — an arrow in a comment
        # explaining a data flow is not a button label, and a check that cannot tell them apart is a
        # check people delete.
        for path in targets:
            if not os.path.exists(path):
                continue
            src = open(path, encoding="utf-8").read()
            if path.endswith(".java"):
                src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
                src = re.sub(r"//[^\n]*", " ", src)
                shown = re.findall(r'"((?:[^"\\]|\\.)*)"', src)
            else:
                src = re.sub(r"<!--.*?-->", " ", src, flags=re.S)
                shown = re.findall(r"<string[^>]*>(.*?)</string>", src, re.S)
            for text in shown:
                m = bad.search(text)
                self.assertIsNone(m, "%s shows %r in %r — use a sprite icon"
                                  % (os.path.basename(path), m.group(0) if m else "", text[:60]))


if __name__ == "__main__":
    unittest.main()
