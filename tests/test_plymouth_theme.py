"""The PosterChanOS boot splash — above all, its disk-password prompt.

Reported: "I had to press Esc when the laptop booted to even know that it was waiting for a
password". The theme drew its prompt with `Image.Text`, which needs a label plugin and a font in the
initramfs; Plymouth 22.02 (amd64 stable, what every PosterChanOS machine runs) ships neither there —
MEASURED with `dracut -m "base plymouth"` + lsinitrd: script.so and all theme files, no label.so, no
font. So the prompt hid the spinner and drew nothing. These tests pin the rule that fixes it — the
password screen is pictures — plus the two ways a fix could silently never reach a machine.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "os/plymouth/posterchanos"
SCRIPT = (THEME / "posterchanos.script").read_text(encoding="utf-8")
GENERATOR = ROOT / "os/plymouth/generate_theme.py"
PUBLISH = (ROOT / "scripts/publish_overlay.sh").read_text(encoding="utf-8")
EBUILD_FILES = ROOT / "os/overlay/app-misc/posterchanos-shell/files"


def _code():
    """The script with comments and string contents removed."""
    out = []
    for line in SCRIPT.splitlines():
        line = re.sub(r'"[^"]*"', '""', line)
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def _function(name):
    start = SCRIPT.index(f"fun {name} (")
    depth, i = 0, SCRIPT.index("{", start)
    for j in range(i, len(SCRIPT)):
        depth += {"{": 1, "}": -1}.get(SCRIPT[j], 0)
        if depth == 0:
            return SCRIPT[i:j + 1]
    raise AssertionError(f"unterminated function {name}")


def test_every_image_the_script_loads_is_in_the_theme():
    names = set(re.findall(r'(?:Image|load)\("([^"]+)"\)', SCRIPT))
    assert names, "no images referenced"
    for name in names:
        assert (THEME / name).is_file(), name
    for i in range(36):
        assert (THEME / f"progress-{i}.png").is_file()


def test_the_script_is_balanced():
    code = _code()
    for a, b in ("()", "{}", "[]"):
        assert code.count(a) == code.count(b), (a, code.count(a), code.count(b))


def test_every_callback_the_password_flow_needs_is_registered():
    for fn in ("SetDisplayPasswordFunction(display_password)", "SetDisplayNormalFunction(display_normal)",
               "SetMessageFunction(message)", "SetQuitFunction(quit)", "SetRefreshFunction(refresh)"):
        assert f"Plymouth.{fn}" in SCRIPT, fn


def test_the_password_prompt_is_drawn_from_pictures_not_text():
    """The load-bearing rule. Everything a person needs to see must come from a PNG."""
    body = _function("display_password")
    for part in ("show_prompt(1)", "ask.dots[i].SetOpacity(1)", "ask.hint.SetOpacity(1)",
                 "ask.error.SetOpacity(1)"):
        assert part in body, part
    shown = _function("show_prompt")
    for sprite in ("ask.panel", "ask.title", "ask.field", "ask.lock"):
        assert sprite + ".SetOpacity(on)" in shown, sprite
    for sprite, png in (("ask.panel", "panel.png"), ("ask.title", "prompt-title.png"),
                        ("ask.hint", "prompt-hint.png"), ("ask.error", "prompt-error.png"),
                        ("ask.lock", "lock.png"), ("ask.esc", "prompt-esc.png")):
        assert f'{sprite} = Sprite(load("{png}"))' in SCRIPT, sprite
    assert 'ask.dot_image = load("dot.png")' in SCRIPT
    assert 'ask.field_image = load("field.png")' in SCRIPT


def test_text_is_only_ever_an_optional_extra():
    """Without a label plugin Image.Text yields an empty image; every use must survive that."""
    uses = [m.start() for m in re.finditer(r"Image\.Text\(", SCRIPT)]
    assert uses
    for at in uses:
        window = SCRIPT[at:at + 260]
        assert "GetWidth() > 0" in window, window


def test_a_refused_passphrase_says_so():
    body = _function("display_password")
    assert "global.submitted" in body and "prompt == global.last_prompt" in body
    assert "global.submitted = 1" in _function("display_normal")


def test_the_avatar_is_the_clients_mark():
    assert (THEME / "avatar.png").is_file()
    assert 'load("avatar.png")' in SCRIPT
    assert 'static/posterchan-relay.png' in GENERATOR.read_text(encoding="utf-8")


@pytest.mark.skipif(not (ROOT / "venv-unified/bin/python").exists(), reason="app venv (Pillow with woff2) required")
def test_the_generator_reproduces_the_committed_images():
    from PIL import Image, ImageChops, ImageStat
    with tempfile.TemporaryDirectory() as out:
        run = subprocess.run([str(ROOT / "venv-unified/bin/python"), str(GENERATOR)],
                             env=dict(os.environ, PC_PLYMOUTH_OUT=out), capture_output=True, text=True,
                             timeout=120)
        assert run.returncode == 0, run.stderr
        made = sorted(p.name for p in Path(out).iterdir())
        assert "avatar.png" in made and "panel.png" in made and "dot.png" in made
        for name in made:
            new = Image.open(Path(out) / name).convert("RGBA")
            old = Image.open(THEME / name).convert("RGBA")
            assert new.size == old.size, name
            # FreeType versions may move an antialiased edge by a shade; a real change moves far more.
            diff = ImageStat.Stat(ImageChops.difference(new, old)).mean
            assert max(diff) < 1.5, (name, diff)


def test_installed_machines_get_this_theme_not_a_stale_copy():
    """The overlay installs FILESDIR/plymouth. A committed copy there sat at the Aug 19 theme while
    os/plymouth moved on, so an overlay update re-installed the OLD splash. Publish injects it."""
    assert not (EBUILD_FILES / "plymouth").exists(), "a second copy of the theme will drift"
    assert '"$(dirname "$SRC")/plymouth/posterchanos/"*' in PUBLISH
    assert 'files/plymouth"' in PUBLISH
    ebuild = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("*.ebuild")).read_text()
    assert 'doins "${FILESDIR}"/plymouth/*' in ebuild
    # …and the rebuild that puts it in the initramfs the loader actually boots.
    assert "plymouth-set-default-theme -R posterchanos" in ebuild
