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
    assert 'pc-wayfire-action "pc:cycle:$direction"' in source
    assert "get_tree" not in source, "Sway leaves collapse every PosterChan app into one surface"


def test_helper_emits_exact_direction_tick_and_rejects_unknown_actions(tmp_path):
    log = tmp_path / "calls"
    # The helper execs pc-wayfire-action by ABSOLUTE path -- it is run from a key binding, which is
    # not guaranteed a useful PATH -- so the stub has to stand in at that path.
    bindir = tmp_path / "usr" / "local" / "bin"
    bindir.mkdir(parents=True)
    stub = bindir / "pc-wayfire-action"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$PC_CYCLE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    helper = tmp_path / "pc-window-cycle"
    helper.write_text(HELPERS[0].read_text(encoding="utf-8").replace(
        "/usr/local/bin/pc-wayfire-action", str(stub)), encoding="utf-8")
    helper.chmod(0o755)
    env = dict(os.environ, PC_CYCLE_LOG=str(log))
    ok = subprocess.run([str(helper), "previous"], env=env, check=False)
    bad = subprocess.run([str(helper), "sideways"], env=env, check=False)
    assert ok.returncode == 0 and bad.returncode == 2
    assert log.read_text(encoding="utf-8").strip() == "pc:cycle:previous"


def test_renderer_cycles_stable_window_order_in_both_directions():
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "const rows=_switchRows()" in body
    assert "direction==='previous'?-1:1" in body
    assert "(current+step+rows.length)%rows.length" in body
    switcher = CLIENT[CLIENT.index("let _altSwitch=null"):CLIENT.index("// ---- snapping", start)]
    assert "_focusSwitchRow(target)" in switcher
    assert "focusWin(e.win,false)" in switcher


def test_switcher_is_visible_staged_and_never_draws_an_empty_card():
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "className='os-alt-switch'" in body
    assert "className='os-alt-card'+(i===s.index?' selected':'')" in body
    assert "className='os-alt-preview'" in body
    assert "iconSvg(w.icon||'i-grid')" in body
    assert "p.classList.add('empty')" in body
    # 2500ms committed and closed the chooser under anyone who paused to read it.
    assert "setTimeout(()=>_closeAltSwitch(true),5000)" in body
    assert "scrollIntoView({block:'nearest',inline:'nearest'})" in body
    assert "pcWM.preview(key)" in body
    assert "s.nativePreviews" in body
    assert "_altSwitch!==s||!p.isConnected" in body


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
    focus = body.index("_focusCompositorCurrent(shell.id,switchFocusToken)")
    staged = body.index("_drawAltSwitch(_altSwitch)")
    assert shell < focus < staged


def test_compositor_tick_reaches_cycle_handler_and_native_target_uses_normal_focus_path():
    assert "else if(/^pc:cycle:(next|previous)$/.test(p)) cycleWindows(p.slice(9));" in CLIENT
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "if(wins.includes(e.win))focusWin(e.win,false)" in body


def test_alt_tab_crosses_output_boundary_instead_of_wrapping_locally():
    preload = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    assert "cycleOutput: (direction) => ipcRenderer.invoke('pc:wm:cycle-output'" in preload
    assert "ipcMain.handle('pc:wm:cycle-output'" in main
    assert "_shellSurfaces.size<2" in main
    assert "payload:'pc:cycle-enter:'+dir" in main
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    assert "pcWM.cycleOutput(direction)" in body
    # The OTHER output enters the same gesture from the tick, and that is the only `entering:true`
    # left. It used to be reached a second way — a locally-refused handoff restarting the chooser at
    # index 0 — which is what made a single-monitor wrap look like a crash.
    assert "cycleWindows(p.slice(15),true)" in CLIENT
    assert "pc:cycle-enter:(next|previous)" in CLIENT


def test_visual_switcher_runtime_cycles_cancels_and_commits():
    run = subprocess.run(["node", str(SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK Alt+Tab switcher holds" in run.stdout


ONE_MONITOR_SIM = ROOT / "tests/client/alt_tab_one_monitor_sim.js"


def test_visual_switcher_holds_together_on_a_single_monitor():
    """The branch every one-screen machine takes on every wrap, which no test had ever run.

    `alt_tab_switcher_sim.js` hands the renderer a `cycleOutput` that always resolves TRUE. The main
    process answers FALSE for a single output (`_shellSurfaces.size < 2`), and on that answer the
    renderer had already removed the chooser — synchronously — before the IPC round trip came back
    to say no, then rebuilt it from scratch at index 0. On a one-screen desk the switcher therefore
    vanished on every wrap and the selection jumped back to the start instead of moving on by one,
    and a gesture begun while the LAST window had focus drew nothing at all.

    Reported as "alt tab is complete garbage and disappears each time you switch to a new window",
    with every assertion in the multi-monitor sim green the whole time.
    """
    run = subprocess.run(["node", str(ONE_MONITOR_SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK Alt+Tab holds on a single monitor" in run.stdout


def test_the_chooser_is_not_taken_down_before_the_handoff_is_agreed():
    """`cycleOutput` existing is not the same as there being a second monitor, and the answer is a
    promise. Tearing the chooser down first is what made a refusal look like a crash."""
    start = CLIENT.index("function cycleWindows(direction,entering){")
    body = CLIENT[start:CLIENT.index("document.addEventListener('keyup'", start)]
    handoff = body[body.index("const handoff="):body.index("if(!_altSwitch){")]
    assert "_altSwitch===s" in handoff, "a stale gesture could tear down a newer chooser"
    assert "moved&&" in handoff, "the chooser is removed without waiting for the other output"
    assert "s.noHandoff=true" in handoff, "one IPC round trip per keypress on a single monitor"
    # And the local move is drawn on every press, boundary or not.
    assert "_altSwitch.index=(next+rows.length)%rows.length;" in body
    assert "_leaveAltSwitch();Promise.resolve(pcWM.cycleOutput" not in body


def test_the_machines_own_windows_are_rows_not_only_our_frames():
    """Hosting native apps in PosterChan frames is opt-in (`pc_os_host_native`, default OFF), so on
    every shipped desktop Firefox/Telegram/a terminal are compositor windows this shell only keeps a
    TASKBAR row for — `nativeTasks`, never `wins`. The switcher read `wins` alone.

    MEASURED on the real two-monitor machine (build 1.0.1382), Firefox + Telegram + foot on screen
    and drawn in the taskbar: `PCOS.windows()` answered `[]` on BOTH renderers, `swaymsg -t
    send_tick pc:cycle:next` changed nothing but the clock, and a DOM recorder showed the chooser
    being created and removed again 38ms later on the one output that had a single frame.
    """
    start = CLIENT.index("let _altSwitch=null")
    body = CLIENT[start:CLIENT.index("// ---- snapping", start)]
    rows = body[:body.index("function _closeAltSwitch")]
    assert "nativeTasks" in rows, "Alt+Tab cannot see the machine's own windows"
    # One window is one row even while `pc_os_host_native` puts it in both lists.
    assert "if(rows.some(x=>x.native===id))continue;" in rows
    # A stashed compositor window has to come back before it can take the keyboard.
    assert "pcWM.show(r.id)" in body and "_focusNativeDecorated(r.id,focusToken)" in body
    # And the same measurement is available to the main process, which must not hand the gesture
    # to a monitor with nothing on it.
    assert "__canCycle: () => _switchRows().length > 0" in CLIENT


def test_a_handoff_is_refused_by_an_output_with_nothing_to_show():
    """`cycleOutput` used to answer true for any second shell surface and focus it first. The origin
    tears its chooser down on that answer, so an empty monitor ended the gesture with no chooser
    anywhere and the keyboard on a bare desktop."""
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    handler = main[main.index("ipcMain.handle('pc:wm:cycle-output'"):]
    handler = handler[:handler.index("ipcMain.handle('pc:wm:snapshot'")]
    ask = handler.index("surfaceCanCycle(target)")
    focus = handler.index("await wm().focus(Number(target.conId))")
    send = handler.index("pc:cycle-enter:")
    assert ask < focus < send, "the other monitor is focused before it has agreed to take over"
    assert "PCOS.__canCycle" in main
    # A renderer that cannot answer keeps the gesture where it is rather than swallowing it.
    assert "setTimeout(()=>res(false),400)" in main


NATIVE_SIM = ROOT / "tests/client/alt_tab_native_taskbar_sim.js"


def test_the_switcher_reaches_the_machines_windows_at_runtime():
    """The other sims put Firefox in `wins` — the HOSTED shape — so the fixture agreed with the bug.
    This one runs the shipped switcher against the shape every machine actually boots into."""
    run = subprocess.run(["node", str(NATIVE_SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK Alt+Tab reaches the machine's own windows" in run.stdout


TICK_SIM = ROOT / "tests/client/shell_tick_delivery_sim.js"


def test_one_compositor_key_press_reaches_the_page_exactly_once():
    """Both forwarders in main.js registered a `tick` listener on the same socket, so every desktop
    binding arrived at the renderer TWICE — measured on hardware by adding a second `pcWM.onEvent`
    listener through the debugger and sending two ticks by hand: `{"pc:probe-one":2,
    "pc:probe-two":2}`.

    A doubled tick never looks like a doubled tick. Alt+Tab steps two windows per press, runs off
    the end of the list and hands the gesture to the other monitor on the FIRST press; Super opens
    the start menu and closes it again; Print Screen saves two files. Each reads as "the key does
    nothing", and the comment in `pc:wm:subscribe` had described the fix for months without the code
    ever doing it.
    """
    run = subprocess.run(["node", str(TICK_SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK one press is one tick" in run.stdout


def test_only_one_place_forwards_a_tick_to_a_renderer():
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    sub = main[main.index("ipcMain.handle('pc:wm:subscribe'"):]
    sub = sub[:sub.index("ipcMain.handle", 10)] if "ipcMain.handle" in sub[10:] else sub
    assert "if (name === 'tick') continue;" in sub, "the subscribe loop forwards ticks again"
    # `subscribe` must still NAME tick: the socket's list is fixed on first subscription.
    assert "const NAMES = ['window', 'workspace', 'output', 'tick'];" in main
