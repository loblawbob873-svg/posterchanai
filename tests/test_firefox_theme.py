"""The PosterChan Cyberpunk Firefox theme (os/firefox-theme) — readable by MEASUREMENT, not by eye.

"Create a Firefox Theme that is cyberpunk and uses posterchan's colors but is readable. Many dark
themes are not readable. We have to make sure not to interfere with the UI. Include the PosterChan
Logo."

  * every text/background pair Firefox draws from this manifest is WCAG AA (4.5:1); icons, the
    selected-tab line and the focused-field ring are 3:1 (WCAG's non-text rule). Translucent hover
    colours are composited over the toolbar they sit on before measuring;
  * the logo banner sits behind the tab strip, where tab titles CAN run over it, so EVERY pixel of it
    (composited over the frame) is measured against the tab text — not a sample, not the average;
  * the palette is the client's own: read out of static/css/client.css, not retyped;
  * it ships signed or not at all (release Firefox silently discards an unsigned theme — measured),
    so the overlay step that fetches the signed copy is RUN with a stub curl, both ways.

Verified to fail: lowering tab_background_text to #8a8cae (it still clears the plain frame, and
fails over the logo), or re-saving the banner at full strength, turns this red.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME = ROOT / "os" / "firefox-theme"
MANIFEST = json.loads((THEME / "manifest.json").read_text(encoding="utf-8"))
COLORS = MANIFEST["theme"]["colors"]
PKG = ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell"
CSS = ROOT / "static" / "css" / "client.css"
PUBLISH = ROOT / "scripts" / "publish_overlay.sh"

try:
    from PIL import Image
except ImportError:  # pragma: no cover - CI installs it; the colour tests do not need it
    Image = None


def parse(c):
    """'#rrggbb' | 'rgba(r, g, b, a)' → (r, g, b, a)."""
    c = c.strip()
    if c.startswith("#"):
        h = c[1:]
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", c)
    assert m, c
    parts = [p.strip() for p in m.group(1).split(",")]
    return tuple(int(p) for p in parts[:3]) + ((float(parts[3]),) if len(parts) > 3 else (1.0,))


def over(fg, bg):
    a = fg[3]
    return tuple(round(fg[i] * a + bg[i] * (1 - a)) for i in range(3)) + (1.0,)


def lum(rgb):
    def ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2])


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def color(key):
    return parse(COLORS[key])


# (foreground, background, what). Every colour here is opaque (asserted below); the translucent
# hover washes are measured separately, composited over the toolbar they sit on.
TEXT = [
    ("tab_background_text", "frame", "background tabs"),
    ("tab_background_text", "frame_inactive", "background tabs, window unfocused"),
    ("tab_text", "tab_selected", "the selected tab"),
    ("toolbar_text", "toolbar", "toolbar labels"),
    ("bookmark_text", "toolbar", "the bookmarks bar"),
    ("toolbar_field_text", "toolbar_field", "the address bar"),
    ("toolbar_field_text_focus", "toolbar_field_focus", "the focused address bar"),
    ("toolbar_field_highlight_text", "toolbar_field_highlight", "selected text in the address bar"),
    ("popup_text", "popup", "menus and panels"),
    ("popup_highlight_text", "popup_highlight", "the highlighted menu item"),
    ("sidebar_text", "sidebar", "the sidebar"),
    ("sidebar_highlight_text", "sidebar_highlight", "the selected sidebar row"),
    ("ntp_text", "ntp_background", "the new tab page"),
]
NON_TEXT = [
    ("icons", "toolbar", "toolbar icons"),
    ("icons", "frame", "tab-strip buttons"),
    ("icons_attention", "toolbar", "an icon asking for attention"),
    ("tab_line", "tab_selected", "the selected-tab line"),
    ("tab_line", "frame", "the selected-tab line against the strip"),
    ("tab_loading", "frame", "the loading indicator"),
    ("toolbar_field_border_focus", "toolbar", "the focused field's ring"),
]


class TheManifest(unittest.TestCase):
    def test_it_is_a_static_theme_firefox_will_accept(self):
        self.assertEqual(MANIFEST["manifest_version"], 2)
        gecko = MANIFEST["browser_specific_settings"]["gecko"]
        self.assertEqual(gecko["id"], "cyberpunk-theme@poster.place")
        self.assertRegex(MANIFEST["version"], r"^\d+\.\d+\.\d+$")
        self.assertNotIn("permissions", MANIFEST, "a theme asks for nothing")
        self.assertNotIn("background", MANIFEST)
        for key in ("frame", "tab_background_text"):
            self.assertIn(key, COLORS, "Firefox requires %s in a theme" % key)

    def test_every_colour_parses(self):
        for k, v in COLORS.items():
            with self.subTest(key=k):
                parse(v)

    def test_every_text_pair_is_AA(self):
        for fg, bg, what in TEXT:
            with self.subTest(pair=what):
                r = ratio(color(fg), color(bg))
                self.assertGreaterEqual(r, 4.5, "%s: %s on %s is %.2f:1" % (what, COLORS[fg], COLORS[bg], r))

    def test_every_non_text_pair_is_3_to_1(self):
        for fg, bg, what in NON_TEXT:
            with self.subTest(pair=what):
                r = ratio(color(fg), color(bg))
                self.assertGreaterEqual(r, 3.0, "%s: %.2f:1" % (what, r))

    def test_hovered_and_pressed_buttons_keep_their_labels_readable(self):
        tb = color("toolbar")
        for key in ("button_background_hover", "button_background_active"):
            with self.subTest(state=key):
                surface = over(color(key), tb)
                self.assertGreaterEqual(ratio(color("toolbar_text"), surface), 4.5)
                self.assertGreaterEqual(ratio(color("icons"), surface), 3.0)

    def test_nothing_that_carries_text_is_translucent(self):
        """A translucent surface is measured against whatever is behind it — which the theme does
        not control. Only the button hover washes (measured above, over the opaque toolbar) and the
        field border may be translucent."""
        allowed = {"button_background_hover", "button_background_active", "toolbar_field_border"}
        for k, v in COLORS.items():
            if k in allowed:
                continue
            with self.subTest(key=k):
                self.assertEqual(parse(v)[3], 1.0, k + " is translucent")

    def test_it_does_not_fight_the_system_about_pages(self):
        """The browser's own chrome is dark; web pages follow the OS preference PosterChanOS now
        sets from the desktop theme (desktop/colorscheme.js) rather than being forced either way."""
        props = MANIFEST["theme"]["properties"]
        self.assertEqual(props["color_scheme"], "dark")
        self.assertEqual(props["content_color_scheme"], "system")


class ThePaletteIsPosterChans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        css = CSS.read_text(encoding="utf-8")
        root = css[css.index(":root{"):]
        root = root[:root.index("\n}\n")]
        cls.tok = {k: v.lower() for k, v in re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", root)}

    def test_the_tokens_are_the_clients(self):
        t = self.tok
        self.assertEqual(COLORS["frame"], t["--bg"])
        self.assertEqual(COLORS["ntp_background"], t["--bg"])
        self.assertEqual(COLORS["toolbar_field"], t["--bg"])
        self.assertEqual(COLORS["popup"], t["--bg2"])
        self.assertEqual(COLORS["sidebar"], t["--bg2"])
        self.assertEqual(COLORS["tab_text"], t["--text"])
        self.assertEqual(COLORS["toolbar_text"], t["--text"])
        self.assertEqual(COLORS["tab_line"], t["--neon"])
        self.assertEqual(COLORS["toolbar_field_border_focus"], t["--neon"])
        self.assertEqual(COLORS["toolbar_field_highlight"], t["--neon"])
        self.assertEqual(COLORS["tab_loading"], t["--neon2"])
        self.assertEqual(COLORS["icons_attention"], t["--neon2"])
        # The focused-window frame colour the compositor uses, so a selected menu row in Firefox is
        # the same colour as a focused window around it.
        self.assertEqual(COLORS["popup_highlight"], t["--frame-focus"])


@unittest.skipIf(Image is None, "Pillow is unavailable")
class TheLogo(unittest.TestCase):
    def banner(self):
        return Image.open(THEME / MANIFEST["theme"]["images"]["theme_frame"]).convert("RGBA")

    def test_tab_titles_read_on_every_pixel_of_it(self):
        b = self.banner()
        frame = color("frame")
        text = color("tab_background_text")
        icons = color("icons")
        worst_t, worst_i = 99.0, 99.0
        data = b.get_flattened_data() if hasattr(b, "get_flattened_data") else b.getdata()
        for r, g, bl, a in data:
            if not a:
                continue
            px = over((r, g, bl, a / 255.0), frame)
            worst_t = min(worst_t, ratio(text, px))
            worst_i = min(worst_i, ratio(icons, px))
        self.assertGreaterEqual(worst_t, 4.5, "a background tab's title over the logo is %.2f:1" % worst_t)
        self.assertGreaterEqual(worst_i, 3.0)

    def test_it_stays_in_the_tab_strip(self):
        """Firefox draws theme_frame at the top-right. Anything taller than the strip would run
        behind the toolbar, which is opaque, so it would never show — keep it to the strip."""
        self.assertLessEqual(self.banner().height, 44)
        self.assertEqual(color("toolbar")[3], 1.0)
        self.assertNotIn("additional_backgrounds", MANIFEST["theme"]["images"])

    def test_it_is_actually_there(self):
        """A watermark dimmed to nothing would pass the readability test trivially."""
        b = self.banner()
        visible = sum(1 for p in (b.get_flattened_data() if hasattr(b, "get_flattened_data") else b.getdata())
                      if p[3] > 100)
        self.assertGreater(visible, 400)

    def test_the_icon_is_the_posterchan_logo(self):
        logo = Image.open(ROOT / "static" / "icon-512.png").convert("RGB").resize((128, 128), Image.LANCZOS)
        icon = Image.open(THEME / MANIFEST["icons"]["128"]).convert("RGBA")
        # Compare inside the rounded mask only.
        diffs = [abs(a - b) for (x, y) in ((x, y) for y in range(24, 104) for x in range(24, 104))
                 for a, b in zip(logo.getpixel((x, y)), icon.getpixel((x, y))[:3])]
        self.assertLess(sum(diffs) / len(diffs), 4.0)


class ThePackage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def build(self, out):
        r = subprocess.run(["python3", str(THEME / "build.py"), str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return Path(r.stdout.strip())

    def test_the_xpi_is_complete_and_reproducible(self):
        a = self.build(self.tmp / "a")
        b = self.build(self.tmp / "b")
        self.assertEqual(a.name, "posterchan-cyberpunk-%s.xpi" % MANIFEST["version"])
        self.assertEqual(a.read_bytes(), b.read_bytes(), "the same sources gave different bytes")
        names = zipfile.ZipFile(a).namelist()
        self.assertEqual(names[0], "manifest.json", "the manifest must be at the root")
        for rel in list(MANIFEST["icons"].values()) + list(MANIFEST["theme"]["images"].values()):
            self.assertIn(rel, names)

    def signed_check(self, path):
        return subprocess.run(["python3", str(THEME / "build.py"), "--signed", str(path)]).returncode

    def test_an_unsigned_build_is_known_to_be_unsigned(self):
        self.assertEqual(self.signed_check(self.build(self.tmp)), 1)
        fake = self.tmp / "s.xpi"
        with zipfile.ZipFile(fake, "w") as z:
            z.writestr("manifest.json", "{}")
            z.writestr("META-INF/cose.sig", b"x")
        self.assertEqual(self.signed_check(fake), 0)
        (self.tmp / "junk.xpi").write_text("not a zip")
        self.assertEqual(self.signed_check(self.tmp / "junk.xpi"), 1)

    def test_the_policy_with_the_theme_is_the_policy_without_it_plus_the_theme(self):
        base = json.loads((PKG / "files" / "firefox-policies.json").read_text())["policies"]
        theme = json.loads((PKG / "files" / "firefox-policies-theme.json").read_text())["policies"]
        for k, v in base["Preferences"].items():
            self.assertEqual(theme["Preferences"].get(k), v, "the theme variant dropped " + k)
        active = theme["Preferences"]["extensions.activeThemeID"]
        self.assertEqual(active["Value"], MANIFEST["browser_specific_settings"]["gecko"]["id"])
        self.assertEqual(active["Status"], "default", "locked would take the choice away from the person")
        self.assertEqual(theme["Extensions"]["Install"], ["file://@THEME_XPI@"])
        self.assertNotIn("ExtensionSettings", theme, "normal/force_installed would forbid removing it")

    def test_the_ebuild_writes_the_theme_policy_only_when_the_signed_file_is_there(self):
        eb = [f for f in os.listdir(PKG) if f.endswith(".ebuild")][0]
        src = (PKG / eb).read_text(encoding="utf-8")
        body = src[src.index("local theme=("):src.index("\n}\n", src.index("local theme=("))]
        self.assertIn('if [[ -f ${theme[0]} ]]; then', body)
        self.assertIn("/usr/share/posterchanos/firefox/${theme[0]##*/}", body)
        self.assertIn('"${FILESDIR}/firefox-policies-theme.json"', body)
        self.assertIn('newins "${FILESDIR}/firefox-policies.json" policies.json', body.split("else", 1)[1])

    def _publish_block(self, curl_body):
        src = PUBLISH.read_text(encoding="utf-8")
        block = src[src.index("# >>> firefox theme"):src.index("# <<< firefox theme")]
        stubs = self.tmp / "bin"; stubs.mkdir()
        (stubs / "curl").write_text("#!/bin/sh\n" + curl_body)
        (stubs / "curl").chmod(0o755)
        tmp = self.tmp / "stage"; files = tmp / "app-misc" / "posterchanos-shell" / "files"
        files.mkdir(parents=True)
        (files / "posterchan-cyberpunk-0.0.1.xpi").write_text("stale")
        script = self.tmp / "block.sh"
        script.write_text("set -euo pipefail\nSRC=%s\nTMP=%s\n%s" % (ROOT / "os" / "overlay", tmp, block))
        env = dict(os.environ, PATH="%s:%s" % (stubs, os.environ["PATH"]))
        r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return sorted(p.name for p in files.iterdir()), r

    def _signed_zip(self):
        p = self.tmp / "signed.xpi"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("manifest.json", "{}")
            z.writestr("META-INF/mozilla.rsa", b"sig")
        return p

    def test_publish_injects_a_signed_theme(self):
        signed = self._signed_zip()
        files, r = self._publish_block(
            'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; esac; shift; done\n'
            'cp "%s" "$out"\n' % signed)
        self.assertEqual(files, ["posterchan-cyberpunk-%s.xpi" % MANIFEST["version"]],
                         "the stale version must go and the current one arrive")
        self.assertIn("(signed)", r.stdout)

    def test_publish_refuses_an_unsigned_one(self):
        unsigned = self.tmp / "u.xpi"
        with zipfile.ZipFile(unsigned, "w") as z:
            z.writestr("manifest.json", "{}")
        files, r = self._publish_block(
            'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; esac; shift; done\n'
            'cp "%s" "$out"\n' % unsigned)
        self.assertEqual(files, [])
        self.assertIn("no SIGNED", r.stderr)

    def test_publish_survives_no_release_yet(self):
        files, r = self._publish_block("exit 22\n")
        self.assertEqual(files, [])
        self.assertIn("shipping without the Firefox theme", r.stderr)

    def test_a_theme_change_triggers_an_overlay_publish(self):
        sync = (ROOT / "sync.sh").read_text(encoding="utf-8")
        self.assertIn("firefox-theme/", re.search(r"grep -qE '\^os/\(([^)]*)\)'", sync).group(1))


if __name__ == "__main__":
    unittest.main()
