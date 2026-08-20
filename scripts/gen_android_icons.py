#!/usr/bin/env python3
"""ONE SET OF ICONS FOR TWO RENDERERS.

The launcher, the dialer and the SMS app are drawn by Android, not by the WebView — that is what
makes them survive a dead renderer, and it is also what cuts them off from `static/js/client/sprite.js`,
the 24x24 stroked sprite every button in the web client uses. Redrawing those glyphs by hand for
Android is how two icon sets drift until the phone's Messages screen and the app's Messages screen
look like different products.

So they are TRANSCRIBED, not redrawn: this reads the sprite's `<symbol>` markup and emits an Android
VectorDrawable per icon, keeping the same 24 grid, the same 1.7 stroke, the same round caps and
joins. `tests/test_android_icon_sprite.py` re-runs it and fails when a checked-in drawable no longer
matches — which is the only thing that can keep two copies of a shape honest.

Colour is deliberately NOT baked: every path strokes white and the views tint at runtime from the
active PcTheme palette, so nine themes cost nine tints rather than nine icon sets.

    python3 scripts/gen_android_icons.py [--check]
"""
import os
import re
import sys
import math

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPRITE = os.path.join(ROOT, "static", "js", "client", "sprite.js")
OUT = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res", "drawable")
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                    "place", "poster", "app", "home", "TileIcons.java")

# The icons the native screens actually use. Deliberately a list rather than "everything": each one
# is ~1KB of XML in the APK and the sprite carries well over a hundred.
WANTED = [
    # launcher chrome
    "search", "play", "pause", "home", "grid", "menu", "eye",
    # every PosterChan view the launcher can put on the home screen, so a tile is the same glyph
    # as its sidebar row in the app
    "ai", "bell", "globe", "speech", "user", "users", "clock", "mail", "bookmark", "phone",
    "translate", "note", "key", "draft", "tv", "git", "news", "chart", "bars", "article",
    "bag", "chat", "magnet", "pawn", "hash", "target", "discs", "cards", "spade", "gamepad",
    "flower", "refresh", "gear", "folder", "music", "calendar", "terminal", "compass",
    # sms
    "send", "plus", "arrow-left", "trash", "paperclip", "image", "check", "close",
    "cloud", "shield", "warn", "reply", "star",
    # dialer
    "call", "mic", "volume", "pin", "logout",
]

STROKE = "1.7"


def symbols(text):
    """Every `<symbol id="i-x" …>…</symbol>` in the sprite, as {name: inner markup}."""
    out = {}
    for m in re.finditer(r'<symbol id="i-([a-z0-9-]+)"[^>]*>(.*?)</symbol>', text, re.S):
        out[m.group(1)] = m.group(2)
    return out


def attrs(tag):
    return dict(re.findall(r'([a-zA-Z-]+)="([^"]*)"', tag))


def rounded_rect(x, y, w, h, rx):
    """An SVG <rect rx> as path data. Android's VectorDrawable has no rect element."""
    if rx <= 0:
        return f"M{x},{y} h{w} v{h} h{-w} z"
    rx = min(rx, w / 2.0, h / 2.0)
    return (f"M{x + rx},{y} h{w - 2 * rx} a{rx},{rx} 0 0 1 {rx},{rx} "
            f"v{h - 2 * rx} a{rx},{rx} 0 0 1 {-rx},{rx} "
            f"h{-(w - 2 * rx)} a{rx},{rx} 0 0 1 {-rx},{-rx} "
            f"v{-(h - 2 * rx)} a{rx},{rx} 0 0 1 {rx},{-rx} z")


def circle(cx, cy, r):
    """Two half-arcs — a full-circle arc is degenerate and renders nothing."""
    return (f"M{cx - r},{cy} a{r},{r} 0 1 0 {2 * r},0 a{r},{r} 0 1 0 {-2 * r},0 z")


def num(s, dflt=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return dflt


def paths(inner):
    """[(pathData, filled, strokeWidth)] for one symbol, in draw order."""
    out = []
    for m in re.finditer(r'<(path|circle|rect)\b([^>]*)/?>', inner):
        kind, raw = m.group(1), m.group(2)
        a = attrs(raw)
        filled = a.get("fill") == "currentColor" and a.get("stroke") == "none"
        width = a.get("stroke-width", STROKE)
        if kind == "path":
            d = a.get("d", "")
        elif kind == "circle":
            d = circle(num(a.get("cx")), num(a.get("cy")), num(a.get("r")))
        else:
            d = rounded_rect(num(a.get("x")), num(a.get("y")),
                             num(a.get("width")), num(a.get("height")), num(a.get("rx")))
        if d:
            out.append((d.strip(), filled, width))
    return out


def vector(name, rows):
    body = []
    for d, filled, width in rows:
        if filled:
            body.append(f'    <path\n        android:fillColor="#FFFFFFFF"\n'
                        f'        android:pathData="{d}" />')
        else:
            body.append(f'    <path\n        android:fillColor="#00000000"\n'
                        f'        android:strokeColor="#FFFFFFFF"\n'
                        f'        android:strokeWidth="{width}"\n'
                        f'        android:strokeLineCap="round"\n'
                        f'        android:strokeLineJoin="round"\n'
                        f'        android:pathData="{d}" />')
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        f"<!-- GENERATED from static/js/client/sprite.js #i-{name} by scripts/gen_android_icons.py.\n"
        "     Do not edit: the web client and the native screens must draw the same glyph, and this\n"
        "     file is how that is enforced (tests/test_android_icon_sprite.py). Colour is applied at\n"
        "     runtime from the active PcTheme palette, so nothing here is themed. -->\n"
        "<vector xmlns:android=\"http://schemas.android.com/apk/res/android\"\n"
        "    android:width=\"24dp\"\n"
        "    android:height=\"24dp\"\n"
        "    android:viewportWidth=\"24\"\n"
        "    android:viewportHeight=\"24\"\n"
        "    android:tint=\"#FFFFFFFF\">\n"
        + "\n".join(body) + "\n</vector>\n"
    )


def tile_icons():
    """A sprite name -> R.drawable switch.

    Written out rather than looked up with getIdentifier(), which is a runtime string lookup invisible
    to lint and to the resource shrinker and which answers 0 for a typo — i.e. an icon that silently
    does not draw. Generated here so an icon added to the sprite and pulled into WANTED reaches the
    native screens with nothing to wire by hand.
    """
    rows = "\n".join(
        '        if ("%s".equals(icon)) return R.drawable.ic_pc_%s;' % (n, n.replace("-", "_"))
        for n in sorted(WANTED))
    return (
        "package place.poster.app.home;\n\n"
        "import place.poster.app.R;\n\n"
        "/**\n"
        " * A SPRITE NAME -> A DRAWABLE.\n"
        " *\n"
        " * GENERATED by scripts/gen_android_icons.py beside the drawables themselves; do not edit.\n"
        " * A switch rather than getIdentifier() because that is a runtime string lookup, invisible to\n"
        " * lint and to the resource shrinker, which answers 0 for a typo -- an icon that silently does\n"
        " * not draw. This one is checked by the compiler.\n"
        " */\n"
        "public final class TileIcons {\n\n"
        "    private TileIcons() { }\n\n"
        "    /** 0 when there is no such icon -- the caller draws nothing rather than crashing on a bad id. */\n"
        "    public static int of(String icon) {\n"
        "        if (icon == null) return 0;\n"
        + rows + "\n"
        "        return 0;\n"
        "    }\n"
        "}\n")


def build():
    text = open(SPRITE, encoding="utf-8").read()
    syms = symbols(text)
    missing = [n for n in WANTED if n not in syms]
    if missing:
        raise SystemExit("sprite has no symbol for: " + ", ".join(missing))
    out = {os.path.join(OUT, f"ic_pc_{n.replace('-', '_')}.xml"): vector(n, paths(syms[n]))
           for n in WANTED}
    out[JAVA] = tile_icons()
    return out


def main():
    check = "--check" in sys.argv
    files = build()
    bad = []
    for path, body in sorted(files.items()):
        name = os.path.basename(path)
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if have == body:
            continue
        if check:
            bad.append(name)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            print("wrote", name)
    if check and bad:
        raise SystemExit("out of date (re-run scripts/gen_android_icons.py): " + ", ".join(bad))
    if check:
        print("android icons match the sprite")


if __name__ == "__main__":
    main()
