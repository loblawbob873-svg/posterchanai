"""The PosterChan app icon is a round badge everywhere it is shown ("postercham needs a proper app icon like you did
for posterfox. the square one is ugly ... that can be the favicon and main icon", 2026-10-29).

What a person sees: a browser tab, a home-screen icon, the desktop app, an Android launcher. Each must be the ROUND
badge (transparent corners, an opaque middle) -- except where something else does the masking (the PWA's maskable
icon, iOS), which must be a full plate or the system crops the badge. Every shipped size must also be what
scripts/make_app_icon.py draws from the committed masters, so a hand-edited file cannot drift out of step.
"""
import importlib.util
import json
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "mobile/android/app/src/main/res"
_spec = importlib.util.spec_from_file_location("make_app_icon", ROOT / "scripts/make_app_icon.py")
MAKER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MAKER)

ROUND = [ROOT / "static/favicon.png", ROOT / "static/icon-192.png", ROOT / "static/icon-512.png"] + \
    [RES / f"mipmap-{d}/{n}.png" for d in MAKER.DENS for n in ("ic_launcher", "ic_launcher_round")]
PLATED = [ROOT / "static/icon-192-maskable.png", ROOT / "static/icon-512-maskable.png", ROOT / "static/apple-touch-icon.png"]


def test_every_app_icon_is_a_round_badge():
    for p in ROUND:
        im = Image.open(p).convert("RGBA")
        w, h = im.size
        assert w == h, p
        a = im.getchannel("A")
        corners = [a.getpixel(xy) for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
        assert corners == [0, 0, 0, 0], ("a square icon again: its corners are painted", p.name, corners)
        assert a.getpixel((w // 2, h // 2)) == 255, ("the middle of the badge is see-through", p.name)


def test_icons_something_else_masks_are_a_full_plate():
    for p in PLATED:
        im = Image.open(p).convert("RGBA")
        assert im.getpixel((0, 0))[:3] == MAKER.NAVY and im.getpixel((0, 0))[3] == 255, p.name


def test_every_shipped_size_is_drawn_from_the_masters():
    for p in ROUND:
        want = MAKER.icon(Image.open(p).size[0])
        diff = ImageStat.Stat(ImageChops.difference(want, Image.open(p).convert("RGBA"))).mean
        assert max(diff) < 1.0, ("not what make_app_icon.py draws from branding/ -- rerun it", p, diff)


def test_pages_and_manifests_point_at_the_right_icons():
    client = (ROOT / "templates/client.html").read_text()
    assert re.search(r'rel="icon"[^>]*href="/static/favicon.png"', client), "the client's tab icon is not the favicon"
    assert 'rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png"' in client
    m = json.loads((ROOT / "static/manifest.json").read_text())
    for i in m["icons"]:
        assert ("maskable" in i["src"]) == (i["purpose"] == "maskable"), ("a round badge offered as maskable is cropped", i)
    bg = (RES / "values/ic_launcher_background.xml").read_text()
    assert "#%02X%02X%02X" % MAKER.NAVY in bg, "the adaptive launcher's plate is not the badge's navy"
