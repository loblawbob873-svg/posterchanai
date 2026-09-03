"""THE SHIPPED SWAY CONFIG MUST PARSE — asked for as "you always ship invalid sway configs first".

A bad line in `sway.config` is not a cosmetic bug. sway loads this file at session start, and on a
FRESH INSTALL that is the only thing between the machine and a desktop: the config fails, sway comes
up with defaults or not at all, and the person is looking at a blank screen with no launcher, no
taskbar and no way to start anything. It has to be right the first time, on a machine nobody can
log into to fix it.

Nothing checked it. Several tests read the file for individual lines (`Mod1+Return`, the float
rules); none asked sway whether the file as a whole is loadable.

sway validates its own config: `sway --validate --config <file>` parses every line and exits
non-zero listing what it could not understand. `WLR_BACKENDS=headless` is needed because sway still
tries to open a DRM session otherwise, which fails for reasons that have nothing to do with the
config. Verified against a deliberately broken copy:

    Error on line 271 'bindsym $mod+Shift+NoSuchKey exec true': Unknown key or button 'NoSuchKey'
    Error on line 272 'for_window [app_id="x" bogus="y"] nonsense_command': Token 'bogus' is not recognized
    Error(s) loading config!                                                        exit 1
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config"
SWAY = shutil.which("sway")

pytestmark = pytest.mark.skipif(not SWAY, reason="sway not installed on this host")


def validate(path: Path):
    """(exit code, config errors) as sway itself reports them."""
    env = dict(os.environ, WLR_BACKENDS="headless")
    done = subprocess.run([SWAY, "--validate", "--config", str(path)],
                          capture_output=True, text=True, timeout=120, env=env)
    errs = [ln for ln in (done.stdout + done.stderr).splitlines() if "[sway/config" in ln]
    return done.returncode, errs


def test_the_shipped_config_loads():
    """THE GUARD. If this fails, a fresh install boots to no desktop."""
    code, errs = validate(CONFIG)
    assert code == 0, "sway refuses the shipped config:\n" + "\n".join(errs[:12])
    assert not errs, "sway reported config errors:\n" + "\n".join(errs[:12])


def test_the_validator_actually_catches_a_bad_line():
    """MUTATION. Without this the check above could be passing because sway never looked — which is
    exactly what happened with `--validate` alone, where a backend failure drowned the result."""
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "sway.config"
        bad.write_text(CONFIG.read_text(encoding="utf-8")
                       + "\nbindsym $mod+Shift+NoSuchKey exec true\n", encoding="utf-8")
        code, errs = validate(bad)
    assert code != 0 and errs, "sway accepted an unknown key — this check proves nothing"


def test_an_unrecognised_criterion_is_caught_too():
    """`for_window` rules are how every window in this desktop gets floated and bordered; a typo in
    one is silent at runtime and the window simply behaves wrongly."""
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "sway.config"
        bad.write_text(CONFIG.read_text(encoding="utf-8")
                       + '\nfor_window [app_id="x" bogus="y"] floating enable\n', encoding="utf-8")
        code, errs = validate(bad)
    assert code != 0 and errs


def test_every_helper_a_binding_runs_is_shipped():
    """A binding that execs a missing helper is a dead key: sway parses it happily and nothing
    happens when it is pressed. The config can only be valid if what it calls exists."""
    import re
    files = ROOT / "os/overlay/app-misc/posterchanos-shell/files"
    text = CONFIG.read_text(encoding="utf-8")
    missing = []
    for helper in sorted(set(re.findall(r"/usr/local/bin/([\w-]+)", text))):
        if not (files / helper).exists() and not (ROOT / "os/bin" / helper).exists():
            missing.append(helper)
    assert not missing, f"bindings call helpers that are not shipped: {missing}"


# ── what `sway --validate` cannot answer ─────────────────────────────────────────────────────────
#
# MEASURED on the live compositor, because validation is blind here and reasoning got it wrong once:
#
#     swaymsg 'exec touch /tmp/a; touch /tmp/b'
#       -> {"success": false, "error": "Unknown/invalid command 'touch'"}   /tmp/a made, /tmp/b NOT
#     swaymsg 'exec "touch /tmp/a; touch /tmp/b"'
#       -> {"success": true}                                               both made
#
# `;` separates SWAY commands, so an unquoted `exec A; B` runs A and then hands B to sway, which
# does not know it. And `sway --validate` does not check binding commands AT ALL — a config whose
# every binding is nonsense validates clean (checked: `bindsym Mod4+z definitely_not_a_command`
# exits 0). So the only thing standing between a shipped config and every Super shortcut silently
# doing half its job is this test.

def _bindings():
    for n, line in enumerate(CONFIG.read_text(encoding="utf-8").splitlines(), 1):
        st = line.strip()
        if st.startswith("bindsym") or st.startswith("bindcode"):
            yield n, st


def test_a_shell_command_containing_a_semicolon_is_quoted():
    """THE BUG THIS FILE EXISTS FOR, second instance. Unquoted, the marker would be written and the
    real command — snap, close, launch a terminal — would never run."""
    for n, st in _bindings():
        if " exec " not in st:
            continue
        cmd = st.split(" exec ", 1)[1].strip()
        if ";" not in cmd:
            continue
        assert cmd.startswith('"') and cmd.rstrip().endswith('"'), (
            f"sway.config:{n} — `exec` with an unquoted `;`. sway splits there and hands the second "
            f"half to itself as a command, so only the first half runs:\n    {st}")


def test_every_helper_a_binding_runs_is_shipped_by_the_package():
    """A binding pointing at a path no ebuild installs is a key that does nothing, on a fresh
    install only — which is the install nobody here tests by hand."""
    import re as _re
    ebuild = next((CONFIG.parent.parent).glob("posterchanos-shell-*.ebuild")).read_text(encoding="utf-8")
    installed = set(_re.search(r"for helper in ([^;]+); do", ebuild).group(1).split())
    for n, st in _bindings():
        for path in _re.findall(r"/usr/local/bin/([a-z0-9-]+)", st):
            assert path in installed, (
                f"sway.config:{n} runs /usr/local/bin/{path}, which the ebuild does not install — "
                f"that binding is dead on a fresh machine")
