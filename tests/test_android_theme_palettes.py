"""NINE THEMES, TWO RENDERERS, ONE PALETTE — and the only thing that can keep them honest.

The launcher, the dialer and the SMS screens are drawn by Android rather than by the WebView. That is
what makes them survive a dead renderer, and the price is that they inherit nothing from
static/css/client.css: no variables, no glow, no colours. So the palettes are transcribed into
place/poster/app/ui/PcTheme.java — a second copy of a value, which in this codebase is the shape that
always drifts.

This test parses the CSS, RUNS the Java, and compares them value for value. A theme edited in the
stylesheet and not in the Java fails here rather than showing up months later as "my phone's Messages
app is still the old pink".
"""
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "css", "client.css")
APPJS = os.path.join(ROOT, "static", "js", "client", "app.js")
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java")
PCTHEME = os.path.join(JAVA, "place", "poster", "app", "ui", "PcTheme.java")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

# The Java field each CSS variable is transcribed into.
FIELDS = [
    ("bg", "--bg"), ("bg2", "--bg2"), ("panel", "--panel"), ("panel2", "--panel2"),
    ("text", "--text"), ("muted", "--muted"), ("line", "--line"),
    ("accent", "--neon"), ("accent2", "--neon2"),
    ("green", "--green"), ("amber", "--amber"), ("danger", "--danger"),
    ("amb1", "--amb1"), ("amb2", "--amb2"),
]

HARNESS = r"""
import place.poster.app.ui.PcTheme;

public class ThemeDump {
  static String hex(int v) { return String.format("%08x", v); }
  public static void main(String[] a) {
    for (String slug : PcTheme.SLUGS) {
      PcTheme.Palette p = PcTheme.of(slug);
      System.out.println(slug
        + "\tbg=" + hex(p.bg) + "\tbg2=" + hex(p.bg2)
        + "\tpanel=" + hex(p.panel) + "\tpanel2=" + hex(p.panel2)
        + "\ttext=" + hex(p.text) + "\tmuted=" + hex(p.muted) + "\tline=" + hex(p.line)
        + "\taccent=" + hex(p.accent) + "\taccent2=" + hex(p.accent2)
        + "\tgreen=" + hex(p.green) + "\tamber=" + hex(p.amber) + "\tdanger=" + hex(p.danger)
        + "\tamb1=" + hex(p.amb1) + "\tamb2=" + hex(p.amb2)
        + "\tr=" + p.radiusDp + "\tneon=" + p.neon + "\tdecor=" + p.decor
        + "\tdark=" + p.isDark());
    }
    // An unknown slug must be the flagship, never a stock grey: "the theme did not load" and "the
    // user chose grey" must not look the same.
    System.out.println("UNKNOWN\t" + PcTheme.of("no-such-theme").slug + "\t" + PcTheme.of(null).slug);
  }
}
"""


def _css_blocks():
    """{slug: {var: value}} straight out of client.css, with :root as 'cyberpunk'."""
    css = open(CSS, encoding="utf-8").read()
    out = {}
    for m in re.finditer(r"^:root(?:\[data-theme=\"([a-z0-9]+)\"\])?\s*\{(.*?)^\}",
                         css, re.S | re.M):
        slug = m.group(1) or "cyberpunk"
        if slug in out:
            continue                                  # the first block wins; later ones are rules
        body = m.group(2)
        vals = {}
        for v in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body):
            vals.setdefault(v.group(1), v.group(2).strip())
        out[slug] = vals
    return out


def _argb(value, inherited):
    """A CSS colour as an 8-hex-digit ARGB string, or None when it is not a colour at all."""
    v = (value or "").strip()
    if v == "transparent":
        return "00000000"
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        return "ff" + m.group(1).lower()
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", v)
    if m:
        d = m.group(1).lower()
        return "ff" + "".join(c * 2 for c in d)
    m = re.fullmatch(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)", v)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # Half-UP, matching java.lang.Math.round — Python's round() is half-to-even, so .30*255
        # (76.5) would come out 76 here and 77 there and the test would report a drift that is not
        # one. The stylesheet is full of two-decimal alphas that land exactly on .5.
        a = int(math.floor(float(m.group(4)) * 255 + 0.5))
        return "%02x%02x%02x%02x" % (a, r, g, b)
    return None


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.exists(PCTHEME), "no android sources here")
class ThemePalettes(unittest.TestCase):
    java = None
    css = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        h = os.path.join(cls.tmp, "ThemeDump.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        r = subprocess.run([JAVAC, "-nowarn", "-d", cls.tmp, "-sourcepath", JAVA, PCTHEME, h],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        r = subprocess.run([JAVARUN, "-cp", cls.tmp, "ThemeDump"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.java = {}
        cls.unknown = ""
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if parts[0] == "UNKNOWN":
                cls.unknown = " ".join(parts[1:])
                continue
            cls.java[parts[0]] = dict(p.split("=", 1) for p in parts[1:])
        cls.css = _css_blocks()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_theme_the_client_offers_exists_in_java(self):
        """A theme added to the picker and not here draws the flagship palette on the phone's home
        screen, its dialer and its Messages app — silently, because an unknown slug falls back."""
        js = open(APPJS, encoding="utf-8").read()
        block = js[js.index("const THEMES = ["):js.index("const THEME_SLUGS")]
        offered = set(re.findall(r"\['([a-z0-9]+)',", block))
        self.assertTrue(offered, "could not read the client's theme list")
        self.assertEqual(offered - set(self.java), set())

    def test_the_colours_match_the_stylesheet(self):
        """Value for value, with CSS inheritance honoured: a theme that declares no --danger really
        does show the flagship's, and the Java must say the same rather than picking a nicer one."""
        root = self.css["cyberpunk"]
        for slug, want in self.java.items():
            block = self.css.get(slug)
            self.assertIsNotNone(block, "client.css has no block for " + slug)
            for field, var in FIELDS:
                raw = block.get(var, root.get(var))
                expect = _argb(raw, root)
                if expect is None:
                    continue                       # not a plain colour (a gradient, a keyword)
                self.assertEqual(want[field], expect,
                                 "%s %s: css %s -> %s, java %s" % (slug, var, raw, expect, want[field]))

    def test_the_corner_radius_matches(self):
        """win98 is 0 and must STAY 0 — square corners are the theme, and a rounded native screen
        beside a square web one is the drift this file exists to catch."""
        root = self.css["cyberpunk"]
        for slug, want in self.java.items():
            raw = self.css[slug].get("--r", root.get("--r"))
            m = re.fullmatch(r"(\d+)(px)?", (raw or "").strip())
            if not m:
                continue
            self.assertEqual(int(want["r"]), int(m.group(1)), slug + " radius")

    def test_only_the_flagship_carries_the_decor(self):
        """client.css hides .city-bg, .scanlines and .grid-bg for every :root[data-theme] — the neon
        grid over Cherry Blossom or Windows 98 does not read as a theme, it reads as a bug."""
        for slug, want in self.java.items():
            self.assertEqual(want["decor"], "true" if slug == "cyberpunk" else "false", slug)

    def test_a_glow_is_never_applied_to_a_light_theme(self):
        """A neon halo behind dark text on a light background destroys it — which is why the
        stylesheet switches every text-shadow off outside the flagship. The native rule is stricter
        and simpler: a palette only glows if its own background is dark."""
        for slug, want in self.java.items():
            if want["neon"] == "true":
                self.assertEqual(want["dark"], "true", slug + " glows on a light background")

    def test_an_unknown_theme_is_the_flagship_not_a_stock_grey(self):
        self.assertEqual(self.unknown, "cyberpunk cyberpunk")


if __name__ == "__main__":
    unittest.main()
