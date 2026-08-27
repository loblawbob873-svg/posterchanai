"""Alt+Tab cycles actual PosterChan app windows as well as native compositor clients."""

from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CLIENT = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
HELPERS = [ROOT / "os/bin/pc-window-cycle",
           ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-cycle"]


def test_installer_and_package_helpers_are_identical_tick_senders():
    assert HELPERS[0].read_bytes() == HELPERS[1].read_bytes()
    source = HELPERS[0].read_text(encoding="utf-8")
    assert 'send_tick "pc:cycle:$direction"' in source
    assert "get_tree" not in source, "Sway leaves collapse every PosterChan app into one surface"


def test_helper_emits_exact_direction_tick_and_rejects_unknown_actions(tmp_path):
    log = tmp_path / "calls"
    stub = tmp_path / "swaymsg"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$PC_CYCLE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
               PC_CYCLE_LOG=str(log))
    ok = subprocess.run([str(HELPERS[0]), "previous"], env=env, check=False)
    bad = subprocess.run([str(HELPERS[0]), "sideways"], env=env, check=False)
    assert ok.returncode == 0 and bad.returncode == 2
    assert log.read_text(encoding="utf-8").strip() == "-t send_tick pc:cycle:previous"


def test_renderer_cycles_stable_window_order_in_both_directions():
    start = CLIENT.index("function cycleWindows(direction)")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "const rows=wins.slice()" in body
    assert "direction==='previous'?-1:1" in body
    assert "(current+step+rows.length)%rows.length" in body
    assert "focusWin(target,false)" in body


def test_switching_from_a_native_app_focuses_shell_before_internal_frame():
    start = CLIENT.index("function cycleWindows(direction)")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    shell = body.index("posterchan(?:-desktop)?")
    focus = body.index("pcWM.focus(shell.id)")
    activate = body.index(".then(activate,activate)")
    assert shell < focus < activate


def test_compositor_tick_reaches_cycle_handler_and_native_target_uses_normal_focus_path():
    assert "else if(/^pc:cycle:(next|previous)$/.test(p)) cycleWindows(p.slice(9));" in CLIENT
    start = CLIENT.index("function cycleWindows(direction)")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "if(target.native!=null){focusWin(target,false);return true;}" in body
