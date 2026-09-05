"""THE START MENU IS ITS OWN FLOATING WINDOW, BECAUSE THE SHELL CAN NEVER BE ON TOP.

Six separate reports were one bug: "start menu is not going over windows", "notifications do not go
over open windows", "volume mixer widget and nostr widget still hide behind the damn windows", "the
new post, reply, modal gets stuck behind windows", "start menu going behind terminal window", and
the Alt+Tab chooser before them.

sway paints floating windows above tiled ones unconditionally, and the desktop shell is the TILED
window. Nothing drawn inside it can be above an application, and no z-index crosses compositor
surfaces. Two workarounds were tried and both were wrong:

  * fullscreening the shell puts it on top and HIDES every other window on the workspace — pressing
    Start emptied the desktop, reported within minutes;
  * hosting the apps inside the shell fixes the stacking and is the code path that breaks fullscreen
    games ("cyberpunk loads in a small window, does not capture mouse").

A separate floating window has neither problem, and the assumption was MEASURED on the real machine
before any of this was written — a brand-new floating window mapped above Telegram and both
PosterChan windows, last in sway's floating stack:

    1. Telegram   2. PosterChan Window — messages   3. PosterChan Window — terminal   4. foot

So the menu is a real surface the compositor stacks for us. The in-page menu stays for every shell
that has no popup bridge — a browser, the Windows and macOS builds — which is why the branch is on
the bridge existing, never on a platform string.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _fn(src: str, decl: str) -> str:
    start = src.index(decl)
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(decl)


# ── the window ───────────────────────────────────────────────────────────────────────────────────

def test_the_main_process_can_open_a_popup_window():
    assert "ipcMain.handle('pc:popup:open'" in MAIN


def test_it_is_frameless_and_not_a_taskbar_entry():
    """A menu with a title bar and a taskbar button is not a menu."""
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:open'"):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    assert "frame: false" in body and "skipTaskbar: true" in body


def test_only_one_popup_exists_at_a_time():
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:open'"):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    assert "closePopupWindow();" in body


def test_clicking_away_closes_it():
    """Otherwise a menu is left stranded on another output with nothing to dismiss it."""
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:open'"):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    assert "p.on('blur'" in body


def test_its_geometry_is_bounded():
    """It is positioned from a renderer's numbers; a bad width must not create a 40000px window."""
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:open'"):]
    assert "Math.max(min, Math.min(max, Math.round(n)))" in body


# ── the channel back ─────────────────────────────────────────────────────────────────────────────

def test_the_popup_can_tell_the_shell_what_was_chosen():
    """It is a separate renderer; it cannot call the desktop's openApp."""
    assert "ipcMain.handle('pc:popup:pick'" in MAIN
    assert "pcPopup" in PRELOAD and "pick:" in PRELOAD


def test_the_choice_travels_on_the_tick_path_the_shell_already_routes():
    """Reusing `pc:` ticks rather than inventing a second control channel — that is how Super opens
    Start and Ctrl+Alt+Del opens the task manager."""
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:pick'"):]
    body = body[:body.index("ipcMain.handle('pc:wm:close'")]
    assert "forwardShellTick(" in body and "'pc:open:'" in body


def test_the_view_name_is_sanitised_before_it_becomes_a_tick():
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:pick'"):]
    assert "replace(/[^a-z0-9_:-]/gi" in body


def test_the_shell_acts_on_that_tick():
    assert "p.indexOf('pc:open:') === 0" in OS_JS
    assert "openLauncherApp(p.slice('pc:open:'.length))" in OS_JS


# ── the popup page ───────────────────────────────────────────────────────────────────────────────

def test_a_popup_never_becomes_a_desktop():
    """Without this it builds a whole second desktop inside a 420x560 window — the exact failure the
    terminal window had."""
    assert "if(popupKind()) return;" in _fn(OS_JS, "  function enter(){")


def test_restore_draws_only_the_menu_in_a_popup():
    body = _fn(OS_JS, "  function restore(){")
    assert "if(popupKind()){" in body and "renderStartPopup()" in body


def test_the_menu_is_built_from_the_sidebar_like_every_other_launcher_surface():
    """Same source as the desktop icons and the in-page menu, so a new feature appears here for free
    and the two can never disagree about what exists."""
    body = _fn(OS_JS, "  function renderStartPopup(){")
    assert "toggleStart(true)" in body, (
        "the window builds its own menu again. There is ONE start menu — the one toggleStart draws, "
        "with folders, machine programs, drive results and local files in it. A second, simpler copy "
        "is what shipped first and it was reported as 'start menu is ass': a flat list of app names "
        "replacing a menu that has all of that.")
    assert "_menuInPopup = true" in body, "the menu would try to open a window from inside its own"


def test_choosing_goes_through_the_bridge():
    """The menu is the real one; only its ACTIONS differ, because a 420px window is not where an
    app, a file or a search result opens."""
    assert "function _menuAct(kind, arg){" in OS_JS
    body = _fn(OS_JS, "  function _menuAct(kind, arg){")
    # Routed through `_popupTell`, which swallows the REJECTION a fire-and-forget bridge call makes
    # off a compositor -- the try/catch round the call never could. The channel is unchanged.
    assert "_popupTell('act'" in body
    assert "encodeURIComponent" in body, (
        "a search query or a file path carries spaces and slashes and crosses a process boundary "
        "as one string")


def test_every_action_that_opens_something_reaches_the_desktop():
    """Each of these opens a window, and it must be the desktop's, not the menu's."""
    for fn, kind in [("  function openLauncherApp(view){", "view"),
                     ("  async function toggleFull(){", "full"),
                     ("  function exit(remember){", "classic")]:
        assert f"_menuAct('{kind}'" in _fn(OS_JS, fn), f"{fn.strip()} does not reach the desktop"


def test_machine_program_is_routed_before_the_popup_closes():
    """Closing the popup destroys its renderer, so it cannot launch a process after close."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    machine = body[body.index("if(b.dataset.app){"):body.index("toggleStart(false); openLauncherApp", body.index("if(b.dataset.app){"))]
    assert "_menuAct('app', b.dataset.app)" in machine
    assert machine.index("_menuAct('app', b.dataset.app)") < machine.index("toggleStart(false)")
    actions = OS_JS.split("else if(p.indexOf('pc:act:') === 0)",1)[1].split("else if(p === 'pc:tasks')",1)[0]
    assert "kind === 'app'" in actions and "PCOSShell.launch(val)" in actions


def test_scanned_program_launch_matrix_runs_end_to_end():
    done = subprocess.run(
        ["node", str(ROOT / "tests/client/start_native_launch_runtime.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "OK installed Start programs launch" in done.stdout


def test_closing_the_menu_in_a_window_closes_the_window():
    """Every handler in the menu ends with `toggleStart(false)`. Stated once, at the top."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    assert "_menuInPopup && force === false" in body
    assert "window.close()" in body


def test_escape_closes_it():
    body = _fn(OS_JS, "  function renderStartPopup(){")
    assert "'Escape'" in body


def test_it_is_searchable_because_it_is_the_real_menu():
    """The search box is the menu's own (`os-q` / `.os-search`), and it searches Nostr, the app
    list, this computer's programs, your drive and local files — none of which the window's first
    implementation had."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    assert 'id="os-q"' in body
    for what in ("This computer", "Your files", "Files on this computer"):
        assert what in body, f"the start menu no longer offers {what!r}"


# ── the fallback ─────────────────────────────────────────────────────────────────────────────────

def test_the_in_page_menu_survives_where_there_is_no_bridge():
    """A browser and the Windows/macOS builds have no compositor and no popup bridge. The branch is
    on the BRIDGE existing, never on a platform string."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    # `toggle`, not `open`: the process holding the window answers whether it is showing.
    # THE GATE, NOT ITS SPELLING. It used to test the `pcPopup` OBJECT, which the preload injects on
    # every platform -- so the plain desktop app on Windows took the compositor-window path with no
    # compositor to place the window, and the in-page panel this test is about was skipped. It asks
    # `_popupWindows()` now: the bridge AND a compositor that answered.
    assert "_popupWindows() && pcPopup.toggle" in body
    assert "_nativeMenuLayer(true)" in body, "the in-page path was removed"
    assert "!_menuInPopup && _popupWindows()" in body, (
        "the menu running INSIDE the window would open a second window from itself")


def test_a_refused_window_does_not_leave_start_stuck_open():
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    assert "startOpen = false; drawBar();" in body


def test_the_popup_has_styling():
    for cls in (".os-popup-body{", ".os-popup-item{", ".os-popup-list{"):
        assert cls in CSS


# ── pressing Super is not a guess ────────────────────────────────────────────────────────────────
#
# Reported as "start menu glitchey as fuck". Measured before the fix, Super six times:
#   menu · nothing · menu · menu · nothing · menu
# and after it, on both monitors:
#   menu · closed · menu · closed · menu · closed
#
# Two causes, both about ownership. The shell decided open-or-close from its own `startOpen` flag,
# which is a GUESS about a window another process owns — the popup also closes on blur, on Escape
# and on every choice, none of which the renderer sees in time. And on two monitors the tick reaches
# BOTH shell surfaces, so the non-focused one called open, destroyed the window the focused one had
# just opened, and only then was declined: a keypress that did everything right and left no menu.

def test_open_or_close_is_decided_by_the_process_that_owns_the_window():
    assert "ipcMain.handle('pc:popup:toggle'" in MAIN
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:toggle'"):]
    body = body[:body.index("ipcMain.handle('pc:popup:open'")]
    assert "_popupKind === k" in body, "toggle does not check what is actually open"
    assert "closePopupWindow();" in body
    assert "toggle: (kind, rect, arg)" in PRELOAD


def test_the_shell_only_paints_what_it_is_told():
    """`startOpen` may not be the decision any more — it is the taskbar highlight."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    assert "pcPopup.toggle('start'" in body
    assert "startOpen = !!open" in body, (
        "the shell still decides from its own flag, which is the every-other-press dead key")


def test_the_surface_that_owns_the_press_is_chosen_before_anything_is_destroyed():
    """The ordering IS the bug: deciding after `closePopupWindow()` meant the second caller killed
    the first caller's window and then declined, leaving nothing."""
    body = MAIN[MAIN.index("async function openPopupWindow("):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    decide = body.index("mine.name !== focused.name")
    destroy = body.index("closePopupWindow();")
    assert decide < destroy, (
        "the output check runs after the existing popup has been closed — on two monitors that "
        "destroys the menu that just opened")


def test_the_window_is_placed_on_the_output_that_asked_for_it():
    """Each surface measures in its own viewport, so local x=10 was global x=10 — always the
    leftmost screen, whichever one the person was looking at."""
    body = MAIN[MAIN.index("async function openPopupWindow("):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    assert "originX" in body and "box.x" in body


def test_a_defensive_close_does_not_shut_a_menu_somebody_just_opened():
    """`toggleStart(false)` is called from several places that mean "make sure it is not showing".
    Forwarding every one of those to the popup closed the window the toggle had just opened."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    at = body.index("_popupTell('close')")
    assert "wasStart" in body[max(0, at - 300):at]


def test_the_menu_gets_the_size_the_menu_needs():
    """420x560 is less than a third of the in-page menu's area and cut the app list off at ten —
    "small as fuck"."""
    body = (_fn(OS_JS, "  function toggleStart(force){") + _fn(OS_JS, "  function _startPopup(){"))
    assert "Math.min(780" in body and "Math.min(920" in body


def test_no_taskbar_toggle_decides_from_a_flag_it_cannot_keep():
    """THE RECURRING ONE, STATED ONCE FOR ALL THREE.

    Start, Notifications and the connectivity panel are all popup WINDOWS on PosterChanOS, and a
    window closes on blur, on Escape and on every choice — none of which the shell renderer is told
    about. So `startOpen` / `notiOpen` / `netOpen` are PAINT flags that drift, and any toggle that
    decides open-or-close from one of them works on alternate presses: "Super six times gave menu,
    nothing, menu, menu, nothing, menu", then "start menu not even functional now too", then the
    tray's connectivity button.

    The rule is that the popup branch is reached BEFORE the flag is read, and the flag is only ever
    written from the answer. Asserted structurally so a fourth panel cannot reintroduce it.
    """
    for name, flag in (("toggleStart(force){", "startOpen"),
                       ("toggleNoti(force){", "notiOpen"),
                       ("toggleNet(force){", "netOpen")):
        body = _fn(OS_JS, "  function " + name)
        helper = ("_startPopup()" if flag == "startOpen"
                  else "_notiPopup()" if flag == "notiOpen" else "_netPopup()")
        assert helper in body, (name, "the popup branch is not reached at all")
        # Everything up to the bridge test. A defensive `force === false` close INSIDE that branch
        # is fine — it is a caller saying "put it away" — but nothing before it may read or write
        # the flag to decide.
        # Up to the GATE, wherever it is spelled: the branch is now reached through
        # `_popupWindows()`, and slicing on the old literal cut at the wrong place -- or not at all.
        head = body[:body.index("_popupWindows()")]
        assert (flag + " =") not in head, (
            name + " decides from " + flag + " before asking the process that owns the window")
        assert ("!" + flag) not in head, (
            name + " branches on " + flag + " before asking the process that owns the window")
