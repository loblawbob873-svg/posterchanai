#!/usr/bin/env python3
"""LAUNCHER ICONS for the Messages and Phone apps, from the same sprite everything else uses.

WHY THEY EXIST AT ALL. Only `.MainActivity` had a MAIN/LAUNCHER filter, so Messages and Phone could
be ROUTED to as the phone's default handlers and appeared in no app drawer — PosterChan's own or the
stock one. Reported as "there is no phone app/icon for it!", and from the person's side that is
simply true: routing is not an app.

They must be DISTINCT. Three drawer entries all showing the PosterChan mark is the same complaint as
the letter tiles — the app is there and looks like it is not — so each gets its own glyph from
`static/js/client/sprite.js` and its own label.

WHAT IS GENERATED, and why each piece:
  * `drawable/ic_app_<n>_fg.xml`   the glyph, centred in the 108 adaptive viewport inside the 72dp
                                   safe zone (anything outside it is cropped by the launcher's mask)
  * `mipmap-anydpi-v26/…xml`       the adaptive icon, for Android 8 and up
  * `mipmap-<density>/…png`        the legacy raster, for 23-25 — WITHOUT these the icon resource
                                   does not resolve on those versions at all
Rasterised with rsvg-convert, which is what the repo already has.

    python3 scripts/gen_android_app_icons.py [--check]
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPRITE = os.path.join(ROOT, "static", "js", "client", "sprite.js")
RES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_android_icons import symbols, paths  # noqa: E402  the one transcriber, shared

# app name -> (sprite symbol, plate colour). The plate is the flagship accent family so the two sit
# together in a drawer and read as one product, with different glyphs so they are not one app twice.
APPS = {
    "messages": ("chat", "#0E2A33"),
    "phone": ("call", "#0E2A33"),
    # EMAIL. "no Email app phone launcher either" — and unlike the other two there is no native
    # activity to alias, so it is a `.shortcut.Email` alias over the view trampoline. Same plate,
    # its own glyph, for the same reason: a drawer must show three different things.
    "email": ("mail", "#0E2A33"),
}
GLYPH = "#3CE8FF"
DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def foreground(name, symbol):
    """The glyph on the 108 grid. Scaled to 44/24 and centred so it sits inside the 72dp safe zone —
    a foreground drawn edge to edge is cropped to a circle by most launchers."""
    rows = paths(symbol)
    scale = 44.0 / 24.0
    off = (108 - 44) / 2.0
    body = []
    for d, filled, width in rows:
        if filled:
            body.append('        <path android:fillColor="%s" android:pathData="%s" />' % (GLYPH, d))
        else:
            body.append('        <path android:fillColor="#00000000" android:strokeColor="%s"\n'
                        '            android:strokeWidth="%s" android:strokeLineCap="round"\n'
                        '            android:strokeLineJoin="round" android:pathData="%s" />'
                        % (GLYPH, width, d))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!-- GENERATED from static/js/client/sprite.js #i-%s by scripts/gen_android_app_icons.py.\n'
        '     Do not edit. The glyph is scaled into the 72dp safe zone of the 108 adaptive grid;\n'
        '     anything outside it is cropped away by the launcher mask. -->\n'
        '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    android:width="108dp" android:height="108dp"\n'
        '    android:viewportWidth="108" android:viewportHeight="108">\n'
        '    <group android:scaleX="%.6f" android:scaleY="%.6f"\n'
        '        android:translateX="%.4f" android:translateY="%.4f">\n'
        '%s\n    </group>\n</vector>\n'
    ) % (symbol, scale, scale, off, off, "\n".join(body))


def adaptive(name, plate):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <background android:drawable="@drawable/ic_app_%s_bg" />\n'
        '    <foreground android:drawable="@drawable/ic_app_%s_fg" />\n'
        '</adaptive-icon>\n' % (name, name))


def plate_drawable(plate):
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!-- GENERATED. The plate behind an app icon. -->\n'
            '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    android:width="108dp" android:height="108dp"\n'
            '    android:viewportWidth="108" android:viewportHeight="108">\n'
            '    <path android:fillColor="%s" android:pathData="M0,0h108v108h-108z" />\n'
            '</vector>\n' % plate)


def svg_for(symbol, plate, px):
    """The legacy raster: plate + glyph, as one SVG for rsvg-convert."""
    rows = paths(symbol)
    body = ['<rect x="0" y="0" width="108" height="108" rx="24" fill="%s"/>' % plate]
    body.append('<g transform="translate(32,32) scale(1.8333)">')
    for d, filled, width in rows:
        if filled:
            body.append('<path d="%s" fill="%s"/>' % (d, GLYPH))
        else:
            body.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" '
                        'stroke-linecap="round" stroke-linejoin="round"/>' % (d, GLYPH, width))
    body.append('</g>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 108 108" '
            'width="%d" height="%d">%s</svg>' % (px, px, "".join(body)))


def build():
    text = open(SPRITE, encoding="utf-8").read()
    syms = symbols(text)
    out = {}
    for name, (symbol, plate) in APPS.items():
        if symbol not in syms:
            raise SystemExit("sprite has no symbol for: " + symbol)
        out[os.path.join(RES, "drawable", "ic_app_%s_fg.xml" % name)] = foreground(name, syms[symbol])
        out[os.path.join(RES, "drawable", "ic_app_%s_bg.xml" % name)] = plate_drawable(plate)
        out[os.path.join(RES, "mipmap-anydpi-v26", "ic_launcher_%s.xml" % name)] = adaptive(name, plate)
    return out, syms


def rasters(syms, check):
    """The 23-25 fallbacks. Without them the icon resource does not resolve on those versions."""
    bad = []
    for name, (symbol, plate) in APPS.items():
        for dens, px in DENSITIES.items():
            path = os.path.join(RES, "mipmap-" + dens, "ic_launcher_%s.png" % name)
            if check:
                if not os.path.exists(path):
                    bad.append(os.path.basename(path) + " (" + dens + ")")
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            svg = svg_for(syms[symbol], plate, px)
            tmp = path + ".svg"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(svg)
            subprocess.run(["rsvg-convert", "-w", str(px), "-h", str(px), "-f", "png",
                            "-o", path, tmp], check=True)
            os.remove(tmp)
            print("wrote", os.path.basename(path), dens)
    return bad


def main():
    check = "--check" in sys.argv
    files, syms = build()
    bad = []
    for path, body in sorted(files.items()):
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if have == body:
            continue
        if check:
            bad.append(os.path.basename(path))
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            print("wrote", os.path.basename(path))
    bad += rasters(syms, check)
    if check and bad:
        raise SystemExit("out of date (re-run scripts/gen_android_app_icons.py): " + ", ".join(bad))
    if check:
        print("app icons match the sprite")


if __name__ == "__main__":
    main()
