"""PosterChanOS sounds like PosterChan: "any way to make those the desktop sounds too".

The overlay ships /usr/share/sounds/posterchan (the cyberpunk chime for messages and the bell, the PosterChan
Alert for alarms, the リンリン ringtone for calls; freedesktop for the rest) and the session makes it the
default -- but only over the STOCK theme or its own earlier write, never over a theme somebody chose. The
session function is RUN here with a fake gsettings, the way test_session_ui_scale runs the scale probe.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "os/bin/pc-compositor-session"
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"
SHELL = ROOT / "os/overlay/app-misc/posterchanos-shell"
PUBLISH = ROOT / "scripts/publish_overlay.sh"

pytestmark = pytest.mark.skipif(not SESSION.exists(), reason="no PosterChanOS tree here")


def _function(src: str, name: str) -> str:
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), src, re.S | re.M)
    assert m, name
    return m.group(0)


def _run(tmp_path, current, mark=None, installed=True):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    store = tmp_path / "gsettings.state"
    store.write_text(f"theme-name='{current}'\n")
    g = bin_dir / "gsettings"
    g.write_text("#!/bin/bash\nS=%s\nif [ \"$1\" = get ]; then grep \"^$3=\" \"$S\" | cut -d= -f2-; exit 0; fi\n"
                 "if [ \"$1\" = set ]; then grep -v \"^$3=\" \"$S\" > \"$S.t\"; echo \"$3='$4'\" >> \"$S.t\"; mv \"$S.t\" \"$S\"; fi\n" % store)
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    theme = tmp_path / "theme"; theme.mkdir(exist_ok=True)
    if installed:
        (theme / "index.theme").write_text("[Sound Theme]\n")
    state = tmp_path / "state"; state.mkdir(exist_ok=True)
    if mark is not None:
        (state / "sound-theme").write_text(mark + "\n")
    script = ("note(){ :; }\nstate_dir=%s\n%s\npc_sound_theme\n" % (state, _function(SESSION.read_text(), "pc_sound_theme")))
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", PC_SOUND_THEME_DIR=str(theme),
               DBUS_SESSION_BUS_ADDRESS="unix:path=/dev/null")
    subprocess.run(["bash", "-c", script], env=env, check=True, timeout=30)
    vals = dict(l.split("=", 1) for l in store.read_text().splitlines() if "=" in l)
    return {k: v.strip("'") for k, v in vals.items()}


def test_the_stock_theme_becomes_posterchan(tmp_path):
    got = _run(tmp_path, "freedesktop")
    assert got["theme-name"] == "posterchan" and got.get("event-sounds") == "true", got


def test_a_theme_somebody_chose_is_left_alone(tmp_path):
    assert _run(tmp_path, "ocean")["theme-name"] == "ocean"


def test_nothing_changes_when_the_theme_is_not_installed(tmp_path):
    assert _run(tmp_path, "freedesktop", installed=False)["theme-name"] == "freedesktop"


def test_both_copies_of_the_session_agree_and_run_it():
    assert SESSION.read_bytes() == PACKAGED.read_bytes()
    assert re.search(r"\(\n\tpc_gtk_text_scale\n\tpc_sound_theme\n", SESSION.read_text())


def test_the_package_installs_the_theme_and_publishing_supplies_the_sounds():
    eb = next(SHELL.glob("posterchanos-shell-*.ebuild")).read_text()
    assert "insinto /usr/share/sounds/posterchan" in eb and 'doins "${FILESDIR}/sounds/index.theme"' in eb
    assert 'doins "${FILESDIR}"/sounds/stereo/*.oga' in eb
    theme = (SHELL / "files/sounds/index.theme").read_text()
    assert "Inherits=freedesktop" in theme and "Directories=stereo" in theme
    pub = PUBLISH.read_text()
    for ev in ("message-new-instant", "bell", "alarm-clock-elapsed", "phone-incoming-call"):
        assert ev in pub, ev
    for src in ("posterchan-chime.ogg", "posterchan-alert.ogg", "posterchan-cyberpunk.ogg"):
        assert src in pub and (ROOT / "static/sounds" / src).exists(), src
