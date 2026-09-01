"""TWO BUTTONS THAT LOOK THE SAME SIZE HAVE TO BE THE SAME SIZE.

Reported as "Edit Settings Hamburger buttons are all different sizes". They were, and it was not
that dialog's fault — it was global:

    .btn        { border: 1px solid var(--line); padding: 8px 14px }   ← outlined
    .btn-neon   { border: none }                                       ← filled
    .btn-cyan   { border: none }
    .btn-red    { border: none }

Identical padding, identical font, and a filled button comes out **2px shorter** than a ghost one
because it drops the border out of its box. In Edit profile that puts `Add audio URL` (ghost, 23px)
beside `Upload pic` (cyan, 21px) in the same row.

The obvious fix — `border-color:transparent`, keeping the box and painting nothing — was tried and
REVERTED. It also makes every filled button 2px WIDER, and that wrapped the meme builder's top bar
onto two rows at 360px; `check_meme_mobile` failed it inside one suite run, because that bar is one
row by design. Six buttons times two pixels was the whole margin.

So this file bounds the difference at the border's own 1px rather than demanding parity. Real parity
means the outline stops being a layout border (an inset box-shadow on `.btn`), which shrinks every
outlined button instead and has its own blast radius — a deliberate change, not a mid-session one.

The phone had a second, worse version of it: `.pf-music-actions .btn` carried a 42px touch floor and
the buttons beside it carried none, so `Upload pic` was a 31px target next to 42px ones — not just
inconsistent but harder to hit, on the smallest screen.

Measured in a real browser against the shipped stylesheet, because this is a question about boxes
and nothing that reads CSS as text can answer it.
"""
import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")

#: One of every filled/outlined variant, same size class, same label length.
VARIANTS = ["btn-neon", "btn-cyan", "btn-red", "btn-ghost", ""]


def heights(size_class="small", width=1280):
    row = "".join(
        f'<button class="btn {v} {size_class}" id="b{i}">Label</button>'
        for i, v in enumerate(VARIANTS))
    html = f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>html,body{{margin:0;height:100%}}{CSS}</style>
    <div class="modal-bg"><div class="modal glass"><div class="row">{row}</div></div></div>
    <pre id="out"></pre><script>requestAnimationFrame(()=>{{
      out.textContent=JSON.stringify([...document.querySelectorAll('.btn')].map(b=>({{
        v:b.className, h:Math.round(b.getBoundingClientRect().height)}})));}});</script>"""
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "b.html"
        page.write_text(html)
        done = subprocess.run([
            CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--window-size={width},800", "--force-device-scale-factor=1",
            "--virtual-time-budget=1200", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-1200:]
        match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert match, done.stdout[-1200:]
        return json.loads(unescape(match.group(1)))


@pytest.mark.parametrize("size_class", ["small", ""])
def test_the_variants_stay_within_a_couple_of_pixels(size_class):
    """A filled button is 2px shorter than an outlined one because `.btn` carries a border and the
    filled variants drop it. That is a real inconsistency and it is NOT worth equalising by giving
    them a transparent border: doing so grows every filled button by 2px in BOTH directions, which
    wrapped the meme builder's one-row toolbar at 360px — caught by check_meme_mobile inside one
    suite run. Six buttons times two pixels.

    So the bound is "close", not "identical". Real parity needs the outline to stop being a layout
    border (an inset box-shadow on `.btn`), which is a deliberate change with its own blast radius.
    This holds the line against anything WORSE than the 1px border already costs."""
    got = heights(size_class)
    tall = [b["h"] for b in got]
    assert max(tall) - min(tall) <= 2, (
        "button variants differ by more than the border: "
        + ", ".join(f"{b['v'].strip()}={b['h']}px" for b in got))


@pytest.mark.parametrize("size_class", ["small", ""])
def test_they_agree_on_a_phone_too(size_class):
    got = heights(size_class, width=380)
    tall = [b["h"] for b in got]
    assert max(tall) - min(tall) <= 2, ", ".join(f"{b['v'].strip()}={b['h']}px" for b in got)


def test_the_filled_variants_do_not_grow_the_button_box():
    """The regression this file now guards from the other side: a transparent border on the filled
    variants is 2px of extra width on every one of them, and a one-row toolbar has no 12px to give."""
    for variant in (".btn-neon{", ".btn-cyan{", ".btn-red{"):
        rule = CSS.split(variant, 1)[1].split("}", 1)[0]
        assert "border-color:transparent" not in rule.replace(" ", ""), (
            f"{variant} carries a transparent border again — that widens every filled button and "
            f"wraps the meme builder's top bar at 360px")


def test_the_profile_sheet_gives_every_button_the_same_touch_target_on_a_phone():
    """The second half: a 42px floor that applied to some buttons in a dialog and not others made
    `Upload pic` a smaller target than the buttons beside it, on the screen where that matters."""
    block = CSS.split("@media(max-width:600px){.pf-music-editor", 1)[1].split("}\n", 1)[0]
    assert "#pf-up" in block, "Upload pic has no touch floor while the music buttons do"
    assert "min-height:42px" in block
