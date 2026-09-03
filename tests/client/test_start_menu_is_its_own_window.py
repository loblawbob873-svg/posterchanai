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
    assert "apps()" in body and "a.off" in body


def test_choosing_goes_through_the_bridge():
    body = _fn(OS_JS, "  function renderStartPopup(){")
    assert "pcPopup.pick(view)" in body


def test_escape_closes_it():
    body = _fn(OS_JS, "  function renderStartPopup(){")
    assert "'Escape'" in body


def test_it_is_searchable_and_enter_runs_the_first_match():
    body = _fn(OS_JS, "  function renderStartPopup(){")
    assert "os-popup-q" in body and "'Enter'" in body


# ── the fallback ─────────────────────────────────────────────────────────────────────────────────

def test_the_in_page_menu_survives_where_there_is_no_bridge():
    """A browser and the Windows/macOS builds have no compositor and no popup bridge. The branch is
    on the BRIDGE existing, never on a platform string."""
    body = _fn(OS_JS, "  function toggleStart(force){")
    assert "if(window.pcPopup && pcPopup.open){" in body
    assert "_nativeMenuLayer(true);" in body, "the in-page path was removed"


def test_a_refused_window_does_not_leave_start_stuck_open():
    body = _fn(OS_JS, "  function toggleStart(force){")
    assert "startOpen = false; drawBar();" in body


def test_the_popup_has_styling():
    for cls in (".os-popup-body{", ".os-popup-item{", ".os-popup-list{"):
        assert cls in CSS
