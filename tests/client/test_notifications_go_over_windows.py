"""THE NOTIFICATION CENTRE IS ITS OWN WINDOW, FOR THE SAME REASON THE START MENU IS.

Reported twice, in these words: "notifications do not go over open windows, you need to test
everything! this is embarrassing" and, after the first attempt, "notifications still hides behind
windows".

The compositor fact is the one the start menu already ran into and it has no workaround inside the
page: sway paints floating windows above tiled ones unconditionally, the desktop shell IS the tiled
window, and no z-index reaches across compositor surfaces. Two things were tried before and both
were wrong — fullscreening the shell puts the panel on top and HIDES every other window on the
workspace (reported within minutes as "why does pressing the start menu hide everything"), and
hosting applications inside the shell is the path that breaks fullscreen games.

So the centre is a real floating surface, and the compositor stacks it.

What makes this more than "open a second window" is that the popup is a SEPARATE RENDERER. It has
the key and the relays (same origin, same bundle) but it is 380px of menu that closes when you click
away, so the things a notification leads to — a thread, a profile, a composer — must happen in the
SHELL. That is the `pcPopup.act` channel, and `noti_popup_route_sim.js` RUNS the shell's half of it
rather than reading it: the actions carry hex ids and pubkeys with colons between them, and
`reply:<id>:<pk>` is the only one with two colons, so it is the only case that can tell a correct
kind split from a wrong one. Split it wrongly and opening a post and a profile still both work —
nothing looks broken until somebody presses Reply.

One rendering, two hosts. The panel is built by one function for both, because a second rendering of
the same list is how the two drift apart silently.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def _decomment(js: str) -> str:
    """Code only. These assertions are about what the branch DOES, and a rule that reads its own
    explanation is a rule that passes because somebody wrote the right word in a comment."""
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", js)


def _fn(decl: str, src: str = "") -> str:
    src = src or OS_JS
    start = src.index(decl)
    depth = 0
    for j in range(src.index("{", start), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(decl)


# ── the shell hands the panel to the compositor ──────────────────────────────────────────────────

def test_the_centre_opens_as_a_window_when_there_is_a_compositor_to_give_it_to():
    """THE BUG, named. Without this branch the panel is appended to the shell's own DOM, which is
    the tiled surface every application floats above."""
    body = _fn("  function toggleNoti(force){") + _fn("  function _notiPopup(){")
    assert "pcPopup.toggle('noti'" in body, (
        "the notification centre is still drawn inside the shell, so it opens underneath every "
        "floating application. `toggle`, not `open`: whether it is showing is answered by the "
        "process that owns the window, never by a flag this renderer remembers — that guess is "
        "what made the bell work every other press")


def test_it_is_anchored_to_the_bell_rather_than_dropped_in_a_corner():
    body = _fn("  function toggleNoti(force){") + _fn("  function _notiPopup(){")
    assert "#os-bell" in body, "the popup no longer opens where the bell is"


def test_the_in_page_panel_survives_for_every_shell_without_a_bridge():
    """A browser, the Windows build, the macOS build. The branch must be on the BRIDGE existing,
    never on a platform string, or the centre disappears everywhere else."""
    body = _fn("  function toggleNoti(force){") + _fn("  function _notiPopup(){")
    # `toggle`, not `open` — see the note below. The branch is still on the BRIDGE existing.
    assert "window.pcPopup && pcPopup.toggle" in body
    assert "buildNotiPanel(false)" in body, "the in-page panel is gone — this is browser-only UI now"
    code = _decomment(body)
    for token in ("PosterChanOS", "process.platform", "navigator.userAgent"):
        assert token not in code, f"the popup path is gated on {token} rather than on the bridge"


def test_opening_the_popup_still_counts_as_reading_them_in_the_shell():
    """The popup is a different renderer, and the bell, the clock badge and the sidebar are painted
    from THIS process. Marking read only in the popup leaves the bell lit over an empty centre."""
    body = _fn("  function toggleNoti(force){") + _fn("  function _notiPopup(){")
    # The helper itself: everything up to the call that hands the window over.
    popup = body[body.index("function _notiPopup(){"):body.index("pcPopup.toggle('noti'")]
    assert "notifsRead" in popup, "the shell never marks them read, so the bell stays lit"
    assert "mailAck" in popup, "the mail count on the clock is never acknowledged"


# ── one panel, two hosts ─────────────────────────────────────────────────────────────────────────

def test_one_builder_serves_both_hosts():
    assert "function buildNotiPanel(inPopup){" in OS_JS, (
        "the popup renders its own copy of the notification list — two renderings of one list is "
        "how they drift apart")


def test_the_builder_does_not_place_the_panel_itself():
    """It ran `root.appendChild(panel)` from the inside. In the popup renderer there is no desktop
    to append to, and appending would put the floating copy back into the surface it exists to
    float above."""
    body = _fn("  function buildNotiPanel(inPopup){")
    assert "root.appendChild(panel)" not in body
    assert body.rstrip().endswith("return panel;\n  }"), "the builder no longer returns the panel"


@pytest.mark.parametrize("what,action", [
    ("a post", "'thread:'"),
    ("a profile", "'profile:'"),
    ("a reply", "'reply:'"),
    ("the mail app", "'view:mail'"),
    ("the notifications app", "'view:notifications'"),
])
def test_everything_that_needs_a_window_is_handed_to_the_shell(what, action):
    """A 380px menu that closes on blur is not where a thread is read or a post is written."""
    body = _fn("  function buildNotiPanel(inPopup){")
    assert action in body, f"opening {what} from the popup does not reach the shell"


def test_the_popup_never_navigates_itself():
    """The failure this prevents is subtle and looks like nothing: the popup renders a whole thread
    inside itself and then closes on blur, so the post flashes and vanishes."""
    body = _fn("  function buildNotiPanel(inPopup){")
    for call in ("PC().openThread", "PC().openProfile", "PC().compose"):
        idx = 0
        while True:
            idx = body.find(call, idx)
            if idx < 0:
                break
            before = body[max(0, idx - 420):idx]
            assert "inPopup" in before, (
                f"{call} is reachable from the popup without an inPopup guard — it would render "
                f"inside the menu and disappear when the menu closes")
            idx += len(call)


def test_the_popup_renderer_is_wired_up():
    body = _fn("  function restore(){")
    assert "renderNotiPopup" in body, "?pcpopup=noti loads the bundle and draws nothing"
    assert "function renderNotiPopup(){" in OS_JS


def test_escape_closes_it():
    body = _fn("  function renderNotiPopup(){")
    assert "Escape" in body


# ── the bridge ───────────────────────────────────────────────────────────────────────────────────

def test_the_action_channel_exists_on_both_sides():
    assert "act: (action, keepOpen)" in PRELOAD, "the popup has no way to reach the shell"
    assert "ipcMain.handle('pc:popup:act'" in MAIN


def test_an_action_is_sanitised_before_it_becomes_a_tick():
    """It arrives from a renderer and is pasted into a tick payload the shell parses."""
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:act'"):]
    body = body[:body.index("});") + 3]
    assert ".replace(" in body and ".slice(0," in body


def test_a_composer_popup_does_not_close_when_you_click_away():
    """Every other popup is a menu and should close on blur. A composer that did would throw away
    what you typed the first time you clicked the desktop or opened a file dialog."""
    assert "STICKY_POPUPS" in MAIN
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:open'"):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    assert "if(!sticky) p.on('blur'" in body


# ── the panel fills its window ───────────────────────────────────────────────────────────────────

def test_the_panel_fills_the_window_it_is_hosted_in():
    """`.os-noti` is written to float in the corner of a desktop — fixed, inset, rounded, shadowed.
    In its own window there is no corner and nothing behind it, and left alone it renders inset and
    clips its own last row."""
    assert ".os-popup-body .os-noti{" in CSS
    rule = CSS.split(".os-popup-body .os-noti{", 1)[1].split("}", 1)[0]
    assert "position:static" in rule
    assert "height:100vh" in rule


# ── the router, RUN ──────────────────────────────────────────────────────────────────────────────

def test_the_shell_performs_what_the_popup_chose():
    """Runs the shipped router — see the module docstring for why reply is the load-bearing case."""
    node = subprocess.run(
        ["node", "noti_popup_route_sim.js", "../../static/js/client/os.js"],
        cwd=ROOT / "tests/client", capture_output=True, text=True, timeout=120)
    assert node.returncode == 0, node.stdout + node.stderr


# ── a popup ADDS to the page, it does not replace it ─────────────────────────────────────────────
#
# Found in the shell log on the real desktop, not in any test:
#
#   [perm] denied persistent-storage app://posterchan/index.html?pcpopup=start
#   "[pc] action failed TypeError: Cannot set properties of null (setting 'onclick')"
#
# A popup loads the WHOLE client — it is the same bundle — and app.js goes on binding its own UI
# after os.js has run. `document.body.innerHTML = ''` deletes the elements those bindings are
# looking for, so boot died, the error net caught it, and "action failed" appeared in the corner of
# the menu. The menu itself drew correctly, which is exactly why this survived being looked at.

def test_a_popup_never_empties_the_page_it_loaded_into():
    for fn in ("  function renderStartPopup(){", "  function renderNotiPopup(){"):
        body = _fn(fn)
        assert "document.body.innerHTML" not in body, (
            f"{fn.strip()} empties the body — that deletes the elements app.js binds after it and "
            f"kills the rest of boot with a null onclick")


def test_the_tray_popup_does_not_either():
    shell = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")
    body = shell[shell.index("  async function openTrayPopup(){"):]
    body = body[:body.index("\n  async function quickPop(")]
    assert "document.body.innerHTML" not in body


def test_they_draw_into_a_host_of_their_own():
    assert "function popupHost(){" in OS_JS
    assert "os-popup-host" in OS_JS


def test_the_client_is_hidden_rather_than_deleted():
    """The point of the fix: the DOM stays, so every binding still finds its element."""
    tight = CSS.replace(" ", "")
    assert ".os-popup-body:not(.os-popup-compose)>*:not(#os-popup-host){display:none!important}" in tight, (
        "the client's DOM is deleted rather than hidden again. The compose popup is the exception "
        "and has its own rule — it shows #modal-root, because the composer IS the client's modal")


def test_the_tray_panel_lands_inside_that_host():
    """`openPop` appends to document.body. Under the rule above, that is a hidden panel in an empty
    window — with nothing thrown and nothing logged."""
    shell = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")
    body = shell[shell.index("  function openPop(anchor, html, opts){"):]
    body = body[:body.index("\n  /* THE CLIENT'S OWN API")]
    assert "getElementById('os-popup-host')" in body, (
        "the tray flyout is appended to a body that CSS hides — the window opens empty")
