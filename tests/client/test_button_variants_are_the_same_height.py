"""TWO BUTTONS THAT LOOK THE SAME SIZE HAVE TO BE THE SAME SIZE.

Reported as "Edit Settings Hamburger buttons are all different sizes". They were, and it was not
that dialog's fault — it was global:

    .btn        { border: 1px solid var(--line); padding: 8px 14px }   ← outlined
    .btn-neon   { border: none }                                       ← filled
    .btn-cyan   { border: none }
    .btn-red    { border: none }

Identical padding, identical font, and a filled button came out **2px shorter** than a ghost one
because it had dropped the border out of its box. In Edit profile that put `Add audio URL` (ghost,
23px) directly beside `Upload pic` (cyan, 21px) in the same row of the same sheet, and the same pair
occurs all over the app. `border-color:transparent` keeps the box and paints nothing.

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
def test_every_button_variant_is_the_same_height(size_class):
    """THE BUG. A filled button and an outlined one, same size class, must occupy the same box."""
    got = heights(size_class)
    tall = {b["h"] for b in got}
    assert len(tall) == 1, (
        "button variants render at different heights: "
        + ", ".join(f"{b['v'].strip()}={b['h']}px" for b in got))


@pytest.mark.parametrize("size_class", ["small", ""])
def test_they_agree_on_a_phone_too(size_class):
    got = heights(size_class, width=380)
    assert len({b["h"] for b in got}) == 1, (
        ", ".join(f"{b['v'].strip()}={b['h']}px" for b in got))


def test_the_filled_variants_keep_a_box_the_same_size_as_the_outlined_one():
    """Names the mechanism so the fix cannot be undone by 'tidying' transparent back to none."""
    for variant in (".btn-neon{", ".btn-cyan{", ".btn-red{"):
        rule = CSS.split(variant, 1)[1].split("}", 1)[0]
        assert "border:none" not in rule.replace(" ", ""), (
            f"{variant} drops the border again — it will render 2px shorter than a ghost button")


def test_the_profile_sheet_gives_every_button_the_same_touch_target_on_a_phone():
    """The second half: a 42px floor that applied to some buttons in a dialog and not others made
    `Upload pic` a smaller target than the buttons beside it, on the screen where that matters."""
    block = CSS.split("@media(max-width:600px){.pf-music-editor", 1)[1].split("}\n", 1)[0]
    assert "#pf-up" in block, "Upload pic has no touch floor while the music buttons do"
    assert "min-height:42px" in block
