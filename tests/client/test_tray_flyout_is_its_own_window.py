"""THE TRAY FLYOUT IS ITS OWN WINDOW — "volume mixer widget and nostr widget still hide behind the
damn windows".

Third instance of one compositor fact, after the start menu and the notification centre: sway paints
floating windows above tiled ones, the desktop shell IS the tiled window, and nothing drawn inside a
page can be above another window. The flyout is now a real floating surface.

What makes the tray cheaper than the other two is that THE WHOLE TRAY IS ONE BUTTON. Network, Tor,
power, the output switcher and the volume mixer are not separate popovers — they are SUB-PANELS that
replace the flyout's body with a back arrow, so they draw inside whichever surface the flyout opened
on. One interception, five panels.

The subtle part is that osshell.js now runs in BOTH renderers, and `closePop()` means different
things in each. In the desktop it removes a panel. In the tray window there is nothing left after
that but an empty rectangle, so it has to mean "close the window" — except when openPop calls it to
clear the way for the panel it is about to draw, which would close the window at the moment it
opened. That is the `internal` argument, and it is the only reason this file is not four lines.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _fn(decl: str, src: str) -> str:
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


def test_the_flyout_is_handed_to_the_compositor():
    """THE BUG. Without this the panel is appended to the shell's own body — the tiled surface."""
    assert "openTrayWindow(b);" in SHELL, (
        "the tray flyout is still drawn inside the shell, so it opens underneath every application")
    assert "pcPopup.open('tray'" in SHELL


def test_one_interception_covers_every_sub_panel():
    """If this ever becomes five branches, someone has misread the design: network/tor/power/mixer
    are sub-panels of the quick flyout and inherit its surface."""
    assert SHELL.count("openTrayWindow(") == 2, (
        "the tray now opens more than one window — the sub-panels are meant to replace the "
        "flyout's body, not to become windows of their own")


def test_the_in_page_flyout_survives_where_there_is_no_bridge():
    body = SHELL[SHELL.index("if(kind === 'quick' && !IN_POPUP"):]
    body = body[:body.index("setTimeout(() => done(_pop), 0);")]
    assert "root.pcPopup && root.pcPopup.open" in body
    assert "quickPop(b)" in body, "the in-page flyout is gone — this is PosterChanOS-only UI now"


def test_the_popup_renderer_draws_the_tray_itself():
    assert "async function openTrayPopup(){" in SHELL
    assert "openTrayPopup" in _fn("  const API = {", SHELL), (
        "openTrayPopup is not exported, so the popup window loads the bundle and draws nothing")
    body = OS_JS[OS_JS.index("if(popupKind()){"):]
    body = body[:body.index("return;")]
    assert "openTrayPopup" in body, "?pcpopup=tray is not routed"


def test_the_window_refreshes_the_readings_before_it_paints():
    """It is a different process from the desktop's: it has no summary yet, and a flyout drawn from
    an empty one shows a muted speaker and no networks."""
    body = _fn("  async function openTrayPopup(){", SHELL)
    assert "await refresh()" in body


# ── the `internal` distinction, which is the whole of the bug ────────────────────────────────────

def test_closing_the_flyout_in_a_window_closes_the_window():
    """Otherwise every action that dismisses the flyout — a power confirm, a Tor toggle — leaves an
    empty rectangle floating over the desktop."""
    body = _fn("  function closePop(internal){", SHELL)
    assert "IN_POPUP && !internal" in body
    assert "root.close()" in body


def test_open_pop_clears_the_way_without_closing_the_window():
    """openPop calls closePop first. Uninstrumented, that closes the tray window at the instant it
    opens — the flyout would flash and vanish, every time, with nothing logged."""
    body = _fn("  function openPop(anchor, html, opts){", SHELL)
    assert "closePop(true)" in body, (
        "openPop closes the tray window it is about to draw into")


def test_every_dismissal_inside_the_flyout_still_means_dismiss():
    """The mirror of the rule above: only openPop may pass `internal`. A stray `closePop(true)` in
    a handler is a flyout that will not close."""
    for m in re.finditer(r"closePop\(true\)", SHELL):
        window = SHELL[max(0, m.start() - 600):m.start()]
        assert ("function openPop(" in window or "if(kind === 'quick' && !IN_POPUP" in window), (
            "closePop(true) outside openPop — that flyout can no longer be dismissed:\n"
            + SHELL[max(0, m.start() - 200):m.start() + 60])


def test_a_press_outside_does_not_close_the_tray_window():
    """In a window the panel IS the surface; clicking away is a blur, which main.js already closes
    on. Keeping the pointerdown listener lets a press on the window's own padding close it."""
    body = _fn("  function openPop(anchor, html, opts){", SHELL)
    at = body.index("_popOff = (e) =>")
    assert "if(!IN_POPUP){" in body[max(0, at - 500):at]


def test_the_renderer_knows_which_surface_it_is():
    assert "const IN_POPUP = (() => {" in SHELL
    body = _fn("  const IN_POPUP = (() => {", SHELL)
    assert "pcpopup" in body


# ── it fills its window ──────────────────────────────────────────────────────────────────────────

def test_the_panel_is_taken_out_of_its_chip_anchored_positioning():
    """`.os-pop` is absolutely positioned beside its chip and positionPop writes left/top into it.
    Under `position:static` those writes are inert, which is what lets the window own the placement."""
    assert ".os-popup-body .os-pop{" in CSS
    rule = CSS.split(".os-popup-body .os-pop{", 1)[1].split("}", 1)[0]
    assert "position:static" in rule


def test_the_tray_flyout_hugs_its_content_instead_of_padding_it_out():
    """A 640px WINDOW HELD A 369px PANEL, AND THE OTHER 271px WERE PAINTED PANEL.

    The window is sized once, tall enough for the network list a sub-panel swaps in, so Quick
    Settings at rest -- four tiles and a slider -- hung a wide band of empty panel under the Volume
    mixer button. Measured on the real desktop: window 640, content 369. The notification centre
    still fills its window (it is one long list); only the tray flyout hugs.
    """
    assert ".os-popup-body.os-tray-popup .os-pop{" in CSS
    rule = CSS.split(".os-popup-body.os-tray-popup .os-pop{", 1)[1].split("}", 1)[0]
    assert "height:auto" in rule, "the flyout still stretches to the whole window"
    assert "max-height:100vh" in rule, "a tall sub-panel must still be allowed the whole window"
    # Its own edge comes back: against a transparent window a square-cornered panel that simply
    # stops looks like a clipped surface rather than a flyout.
    assert "border-radius" in rule


def test_the_tray_flyout_sits_on_the_edge_it_rises_from():
    """AND IT HAS TO BE SAID ON THE HOST, NOT THE BODY.

    `#os-popup-host` is `position:fixed`, so it is out of the body's flow: a flex rule on the body
    moves nothing and the shortened panel stayed pinned to the TOP of its window -- floating ~271px
    above the taskbar, which is a different wrong from the one being fixed. Measured after: panel
    top 271, bottom 640, flush with the window's bottom edge.
    """
    assert ".os-popup-body.os-tray-popup #os-popup-host{" in CSS
    rule = CSS.split(".os-popup-body.os-tray-popup #os-popup-host{", 1)[1].split("}", 1)[0]
    assert "flex-direction:column" in rule
    assert "justify-content:flex-end" in rule
    body_rule = CSS.split(".os-popup-body.os-tray-popup{", 1)[1].split("}", 1)[0]
    assert "background:transparent" in body_rule, (
        "the window is opened transparent; a painted body puts the dead space back")
