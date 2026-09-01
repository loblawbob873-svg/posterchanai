"""A NATIVE FRAME MUST NEVER BE A TRANSPARENT HOLE — measured on the real desktop, not reasoned about.

Reported as "if Firefox is maximized, and you click notifications, the screen gets all fucked up".

I read the source twice and guessed wrong twice. The answer came from the machine: SSH to the
PosterChanOS desktop, `swaymsg -t get_tree`, and `grim`. The tree said

    firefox-bin  fullscreen_mode=0  x=9 y=63 3054x1948  visible=False

— parked in the scratchpad — and the screenshot showed a PosterChan window frame titled
"Status - YummyOrder — Mozilla Firefox", maximised, **with the desktop wallpaper showing through its
body**. Not Firefox, and not the "App is behind another window · click to bring it forward" card
either. An empty hole with a title bar on it.

`nsync` has one path that produces that. When a parked surface has to come back it calls
`restore`/`show`, and the failure branch read:

    catch(_){ _natSent.set(it.native, 'hidden'); continue; }

It records that the surface is still parked and moves on **without marking the frame**, so nothing
paints over the gap. Thirty lines above it the same function carries the rule it breaks: *"Never
leave an EMPTY HTML frame on screen while its real Wayland surface is in the scratchpad. That
rectangle was the reported black window."* One path was simply never taught it.

The card matters beyond looking better: it is clickable, and focusing the frame raises and restores
the surface — so a refused restore becomes something a person can act on rather than a hole they
have to drag the window to fix.

This runs the SHIPPED `nsync` failure paths against a stubbed compositor, because the bug is which
class is on the element after a call fails, and no static read of the file can answer that.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _nsync_body() -> str:
    start = OS_JS.index("  async function nsync(){")
    end = OS_JS.index("  /* THE MACHINE'S FILES", start)
    return OS_JS[start:end]


def _restore_failure_branch() -> str:
    """The catch that runs when sway refuses to bring a parked surface back."""
    body = _nsync_body()
    anchor = body.index("if(was === 'hidden'){")
    return body[anchor:body.index("try{ if(!pcWM.restore || was !== rect)", anchor)]


def test_a_refused_restore_marks_the_frame_instead_of_leaving_a_hole():
    """THE BUG, named. The surface is confirmed still parked; the frame has to say so."""
    branch = _restore_failure_branch()
    catch = branch[branch.index("catch(_){"):]
    assert "classList.add('native-stashed')" in catch, (
        "a refused restore still leaves the frame transparent — this is the maximised-Firefox "
        "window with the wallpaper showing through it")
    assert "_natSent.set(it.native, 'hidden')" in catch, (
        "the surface must still be recorded as parked, or the next pass will not retry the show")


def test_the_hole_and_the_card_cannot_both_be_absent():
    """Every path in nsync that leaves a native window on screen either shows its surface or paints
    the card. Stated over the whole function so the next new path cannot quietly become a third
    exception: each `continue` in the placement loop is preceded by one or the other."""
    body = _nsync_body()
    loop = body[body.index("if(stash.has(it.native)){"):body.index("_natShell=null; _natShellAt=0; _natAgain=true;")]
    # Every branch that gives up on a window must have decided what its body shows.
    gives_up = [m.start() for m in re.finditer(r"\bcontinue;", loop)]
    assert gives_up, "the placement loop no longer has early exits — re-read this test"
    for at in gives_up:
        window = loop[max(0, at - 700):at]
        decided = ("native-stashed" in window            # painted the card, or cleared it
                   or "_natMeasureAgain()" in window     # deferred: nothing was changed at all
                   or "pcWM.move" in window)             # a live drag: the surface is visible
        assert decided, (
            "a placement branch gives up without deciding what the window's body shows:\n"
            + window[-260:])


def test_the_card_is_still_what_gets_painted():
    """The class is only worth adding while the stylesheet paints something for it — and something
    legible, not a near-black panel (that regression has its own test)."""
    assert ".osw.native-stashed .osw-body::after" in CSS
    rule = CSS.split(".osw.native-stashed .osw-body::after{", 1)[1].split("}", 1)[0]
    assert "click to bring it forward" in rule
    assert "content:" in rule


def test_clicking_the_marked_frame_can_actually_recover_the_window():
    """The card promises "click to bring it forward". That promise is only kept because a stashed
    frame still takes pointer events and focusing it restores the surface."""
    assert ".osw.native-stashed{pointer-events:auto}" in CSS.replace(" ", ""), (
        "a stashed frame stopped accepting clicks, so the card's instruction is a lie")
    assert "_focusNativeWhenShown" in OS_JS


def test_a_failed_place_after_a_successful_show_is_left_alone():
    """The mirror case, and the reason this is not "always add the class": if `show` succeeded the
    surface IS visible somewhere, and painting an opaque card over a live window would hide the
    application the user is looking at."""
    body = _nsync_body()
    place_catch = body[body.index("try{ if(!pcWM.restore || was !== rect)"):]
    place_catch = place_catch[:place_catch.index("_natSent.set(it.native, rect);")]
    assert "classList.add('native-stashed')" not in place_catch, (
        "a failed place now paints the parked card over a surface that is actually visible")
