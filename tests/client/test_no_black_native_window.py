"""NO STATE OF A NATIVE WINDOW MAY RENDER BLACK — measured in pixels, not asserted in class names.

Reported: "Firefox turns black with 'click to bring this window forward' when you focus another
window." The string is ours, so the window was in the stashed-with-preview state and the preview it
was painting was black.

There was already a test for this feature. It asserted that the rule exists, that the class name
appears in os.js, and that the words are in the stylesheet — all of which were true the whole time
the screen was black. `client.css` even carries a comment on the rule ABOVE this one saying *"Never
fall back to a near-black empty body: that is indistinguishable from Firefox failing to render"*,
and the preview variant directly below it then set `background-color:var(--panel,#0b0a14)`. Nothing
that reads source text can catch that, because the bug is what the pixels do.

So this file renders the real stylesheet in a real browser and looks at the result. Every state a
native window can be left in is drawn and its BODY is sampled:

  * stashed with no preview        — the loud system card
  * stashed with a capture that is genuinely all black   (the reported failure)
  * stashed with a capture that is transparent
  * stashed with a preview URL that does not load
  * stashed with a real, colourful capture               — must still show it

The rule is one line: the body must not be dark. It is deliberately a property of the OUTPUT and
says nothing about how it is achieved, so it keeps holding when the implementation changes.

`previewIsBlank` — the half that refuses to adopt an opaque black capture, since no stylesheet can
paint over one — is run directly at the bottom of this file under node.
"""
import base64
import json
import re
import shutil
import struct
import subprocess
import tempfile
import zlib
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text()
OSNATIVE = ROOT / "static/js/client/osnative.js"
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

#: Anything at or below this mean luminance (0-255) reads as "a black window" to a person.
DARK = 40


def _png(width, height, rgba):
    """A minimal RGBA PNG built here, so a fixture cannot quietly become a different picture."""
    rows = []
    for y in range(height):
        row = b"\x00"
        for x in range(width):
            row += bytes(rgba(x, y))
        rows.append(row)
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


BLACK_PNG = _data_url(_png(8, 8, lambda x, y: (0, 0, 0, 255)))
CLEAR_PNG = _data_url(_png(8, 8, lambda x, y: (0, 0, 0, 0)))
NEAR_BLACK_PNG = _data_url(_png(8, 8, lambda x, y: (3, 2, 4, 255)))
BRIGHT_PNG = _data_url(_png(8, 8, lambda x, y: (250, 240, 90, 255) if (x + y) % 2 else (30, 90, 200, 255)))
WHITE_PNG = _data_url(_png(8, 8, lambda x, y: (255, 255, 255, 255)))
BROKEN = "data:image/png;base64,notapng"


def _mean_luminance(png_bytes, box):
    """Mean luminance of a region of a screenshot, decoded with Pillow."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB").crop(box)
    pixels = list(img.getdata())
    return sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels) / max(1, len(pixels))


#: The window is given a fixed box and its title bar is hidden, so the BODY is exactly that box and
#: the sample region is known without asking the page. No rAF chain: racing `--dump-dom` was the
#: first version of this and it measured nothing at all.
WIN = dict(x=20, y=20, w=600, h=400)

OS_JS = (ROOT / "static/js/client/os.js").read_text()


def _lift(name):
    """The real function out of os.js — the point of this file is to run the shipped adoption
    path, not a restatement of it that could agree with the bug."""
    import re as _re
    m = _re.search(r"\n  function " + _re.escape(name) + r"\(.*?\n  \}", OS_JS, _re.S)
    assert m, f"{name} is gone from os.js"
    return m.group(0)


def _render(classes, preview=None, adopt=True):
    """Draw one native window in the given state and return the screenshot bytes.

    `adopt=True` hands the capture to the SHIPPED `_nativePreview`, exactly as `nsync()` does, so
    the guard that refuses a blank capture and the stylesheet that paints the result are both in
    the picture. `adopt=False` forces the CSS variable on regardless, which is how the stylesheet
    alone is measured."""
    forced = f"--native-stash-preview:url('{preview}');" if (preview and not adopt) else ""
    handoff = ""
    if preview and adopt:
        handoff = f"_nativePreview({{el:document.querySelector('.osw')}}, {json.dumps(preview)});"
    html = f"""<!doctype html><meta charset="utf-8">
    <style>
      html,body{{margin:0;width:100%;height:100%;background:#101014}}
      {CSS}
      /* The bar is not under test and its height is not worth guessing: hide it so the body IS the
         window box, and the sample region below is exact. */
      .osw-bar{{display:none}}
    </style>
    <div class="os-root"><div class="osw {classes}"
      style="left:{WIN['x']}px;top:{WIN['y']}px;width:{WIN['w']}px;height:{WIN['h']}px;{forced}">
      <div class="osw-body"><div class="osw-slot"></div></div>
    </div></div>
    <script>{OSNATIVE.read_text()}</script>
    <script>
      const NAT = () => window.PCOSNative;
      {_lift("_previewPixels")}
      {_lift("_nativePreview")}
      {handoff}
    </script>"""
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "win.html"
        page.write_text(html)
        shot = Path(td) / "win.png"
        done = subprocess.run([
            CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--window-size=700,500", "--force-device-scale-factor=1",
            "--virtual-time-budget=2500",
            f"--screenshot={shot}", page.as_uri()], text=True, capture_output=True, timeout=60)
        assert done.returncode == 0, done.stderr[-1500:]
        assert shot.exists() and shot.stat().st_size > 500, "chrome produced no screenshot"
        return shot.read_bytes()


def _body_luminance(classes, preview=None, adopt=True):
    png = _render(classes, preview, adopt)
    # Inset past the 1px border and the 12px corner radius.
    box = (WIN["x"] + 16, WIN["y"] + 16, WIN["x"] + WIN["w"] - 16, WIN["y"] + WIN["h"] - 16)
    return _mean_luminance(png, box)


pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")


def test_the_measurement_can_tell_a_black_window_from_a_bright_one():
    """The check before the checks. If this file could not see darkness it would pass through the
    whole bug — which is precisely what the previous test for this feature did."""
    assert _mean_luminance(_png(4, 4, lambda x, y: (0, 0, 0, 255)), (0, 0, 4, 4)) < 1
    assert _mean_luminance(_png(4, 4, lambda x, y: (200, 200, 200, 255)), (0, 0, 4, 4)) > 150


def test_a_stashed_window_with_no_preview_is_the_loud_card():
    """The baseline the other states must not be worse than."""
    assert _body_luminance("native-stashed") > DARK


@pytest.mark.parametrize("name,preview", [
    ("an all-black capture", BLACK_PNG),
    ("a near-black capture", NEAR_BLACK_PNG),
    ("a fully transparent capture", CLEAR_PNG),
    ("a preview that does not decode", BROKEN),
])
def test_no_failed_or_empty_capture_can_paint_a_black_window(name, preview):
    """THE REPORTED BUG, in every shape that produces it, through the SHIPPED adoption path.

    Note what this test does NOT do: it never asks whether the class was applied, whether the
    stylesheet mentions a colour, or whether os.js refused the capture. It hands the capture to the
    real `_nativePreview` and then looks at the screen. Any fix that leaves the user with a black
    rectangle fails here however tidy it is — and either half of the fix alone fails it, which is
    the point: the stylesheet cannot paint over an opaque black PNG, and the guard cannot help a
    preview that decodes fine and is simply painted on a near-black panel."""
    lum = _body_luminance("native-stashed", preview)
    assert lum > DARK, (
        f"a stashed window with {name} rendered at mean luminance {lum:.1f} — that is a black "
        f"window with a label in the corner, which is what was reported")


def test_a_real_capture_is_still_shown():
    """The fix must not turn every preview into a card. A capture with content in it is the point of
    the feature — it is how you tell two stashed windows apart in a glance. Adoption must actually
    happen here, so this also proves the guard is not simply refusing everything."""
    png = _render("native-stashed", BRIGHT_PNG)
    box = (WIN["x"] + 16, WIN["y"] + 16, WIN["x"] + WIN["w"] - 16, WIN["y"] + WIN["h"] - 16)
    assert _mean_luminance(png, box) > DARK


def test_the_stylesheet_alone_never_renders_dark_either():
    """The CSS half on its own, with the variable forced on past the guard. This is the regression
    that shipped: a preview that is present but does not paint left `background-color:var(--panel)`
    showing, and on a dark theme that is a black window."""
    for preview in (CLEAR_PNG, BROKEN):
        assert _body_luminance("native-stashed native-stash-preview", preview, adopt=False) > DARK


def test_the_fullscreen_frame_is_hidden_rather_than_drawn_dark():
    """The other state a native window can be parked in. It must vanish, not become a dark panel."""
    assert ".osw.native-fullscreen-frame{visibility:hidden" in CSS


def test_the_preview_rule_never_reintroduces_a_near_black_fallback():
    """A source check, kept alongside the pixel ones because it names the specific regression: the
    variant is only safe while the card is the last background layer under the capture."""
    rule = CSS.split(".osw.native-stashed.native-stash-preview .osw-body::after{", 1)[1].split("}", 1)[0]
    assert "var(--panel" not in rule, (
        "the preview is painting over the translucent panel colour again — on a dark theme that is "
        "the near-black body the rule above this one forbids")
    assert "linear-gradient(135deg,#174b67" in rule, "the system card is no longer the base layer"


# --------------------------------------------------------------------------- the adoption guard


def _blank(pixels):
    script = ("const n=require(%s);process.stdout.write(JSON.stringify("
              "n.previewIsBlank(Uint8ClampedArray.from(%s))));" % (json.dumps(str(OSNATIVE)),
                                                                   json.dumps(pixels)))
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _flat(r, g, b, a=255, n=64):
    return [r, g, b, a] * n


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("pixels,blank,why", [
    (_flat(0, 0, 0), True, "solid black — the capture that started this"),
    (_flat(2, 3, 2), True, "near-black, which looks identical on screen"),
    (_flat(0, 0, 0, 0), True, "fully transparent composites as black over the body"),
    ([], True, "no pixels at all"),
    ([1, 2, 3], True, "a truncated buffer is not a picture"),
    (_flat(255, 255, 255), False, "a blank white page is a real thing to be looking at"),
    (_flat(120, 120, 120), False, "flat mid-grey is dim but legible, not a black window"),
    (_flat(10, 10, 10)[:-4] + [240, 240, 240, 255], False,
     "mostly dark WITH content in it — a dark-themed window is not a blank one"),
])
def test_a_capture_is_only_refused_when_it_is_both_flat_and_dark(pixels, blank, why):
    """An opaque black PNG covers any card the stylesheet can paint, so this is the half that has to
    catch it. It refuses only what is flat AND dark: refusing a uniformly bright capture would
    replace a truthful preview with a card, and refusing a dark capture that has content in it would
    throw away the preview of every dark-themed application on the desktop."""
    assert _blank(pixels) is blank, why


# --------------------------------------------------------------------------- the label must be seen


def _label_band_luminance(classes, preview=None, adopt=True):
    """Mean luminance of the TOP band of the body, where the parked label now lives."""
    png = _render(classes, preview, adopt)
    box = (WIN["x"] + 16, WIN["y"] + 8, WIN["x"] + WIN["w"] - 16, WIN["y"] + 60)
    return _mean_luminance(png, box)


def test_the_parked_label_is_near_the_top_not_under_a_floating_window():
    """A MAXIMISED parked window fills the screen, and a floating app sits in the MIDDLE of it — so
    a centred label is behind that app and the card reads as wallpaper. Measured on the real desktop
    exactly that way: the body sampled the card's own gradient with no legible text anywhere, and it
    was reported as the screen being broken.

    The band checked here is the top of the body. It has to differ from the card's background, which
    is what having a label (and its pill) there means."""
    top = _label_band_luminance("native-stashed")
    middle = _body_luminance("native-stashed")
    assert abs(top - middle) > 3, (
        f"the top band ({top:.1f}) is indistinguishable from the card body ({middle:.1f}) — the "
        f"label is not there, so a maximised parked window says nothing where it can be seen")


def test_the_label_has_a_plate_of_its_own():
    """Text alone on a gradient is what made this read as wallpaper. The pill gives it an edge."""
    assert ".osw.native-stashed .osw-body::before" in CSS
    rule = CSS.split(".osw.native-stashed .osw-body::before{", 1)[1].split("}", 1)[0]
    assert "border-radius:999px" in rule and "rgba(6,10,20" in rule


def test_the_card_still_never_renders_dark():
    """The label move must not have cost the property this file exists for."""
    assert _body_luminance("native-stashed") > DARK
