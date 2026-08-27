"""Alt+Tab cycles actual PosterChan app windows as well as native compositor clients."""

from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CLIENT = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
SIM = ROOT / "tests/client/alt_tab_switcher_sim.js"
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
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "const rows=_switchRows()" in body
    assert "direction==='previous'?-1:1" in body
    assert "(current+step+rows.length)%rows.length" in body
    switcher = CLIENT[CLIENT.index("let _altSwitch=null"):CLIENT.index("// ---- snapping", start)]
    assert "focusWin(target,false)" in switcher


def test_switcher_is_visible_staged_and_never_draws_an_empty_card():
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "className='os-alt-switch'" in body
    assert "className='os-alt-card'+(i===s.index?' selected':'')" in body
    assert "className='os-alt-preview'" in body
    assert "iconSvg(w.icon||'i-grid')" in body
    assert "p.classList.add('empty')" in body
    assert "setTimeout(()=>_closeAltSwitch(true),2500)" in body
    assert "scrollIntoView({block:'nearest',inline:'nearest'})" in body


def test_switcher_commits_on_alt_release_and_escape_restores_initial_window():
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "e.key==='Alt')_closeAltSwitch(true)" in body
    assert "e.key==='Escape'" in body
    assert "_closeAltSwitch(false)" in body
    assert "const target=commit?s.rows[s.index]:s.initial" in body


def test_switching_from_a_native_app_focuses_shell_before_internal_frame():
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    shell = body.index("posterchan(?:-desktop)?")
    focus = body.index("pcWM.focus(shell.id)")
    staged = body.index("_drawAltSwitch(_altSwitch)")
    assert shell < focus < staged


def test_compositor_tick_reaches_cycle_handler_and_native_target_uses_normal_focus_path():
    assert "else if(/^pc:cycle:(next|previous)$/.test(p)) cycleWindows(p.slice(9));" in CLIENT
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "if(target&&wins.includes(target))focusWin(target,false)" in body


def test_visual_switcher_runtime_cycles_cancels_and_commits():
    run = subprocess.run(["node", str(SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK Alt+Tab switcher holds" in run.stdout
