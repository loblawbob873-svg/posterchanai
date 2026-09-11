"""SCROLLING BACK THROUGH A ROOM MUST NOT KEEP SNAPPING TO THE BOTTOM.

Reported as "my position keeps getting reset when I scroll through a room history".

The mechanism, measured: `setProgrammaticScroll` marks the scroller while it moves it, so `onscroll`
does not read the app's own move as the reader's. The mark was a TIME WINDOW — every scroll event
between setting it and the next animation frame was discarded — and `pinned`, the flag that decides
whether the next content growth snaps to the bottom, is computed from exactly those events. A
picture finishing decryption grows the list, the growth restores the pin, the flick that lands in
that frame is thrown away, `pinned` stays true, and the next picture snaps the reader down again. In
a room full of media that repeats the whole way up.

It is answered by POSITION now, not by time: a scroll event still sitting where the app put the
scroller is the app's, anything else is the reader, and the reader always wins.

THE EXISTING HARNESS COULD NOT SEE ANY OF THIS. `concord_scroll_runtime.mjs` runs
`requestAnimationFrame` synchronously, so the mark is set and cleared inside one call and the window
these tests are about is zero-width — the fixture agreed with the bug. The runtime beside this file
defers frames the way a browser does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def _all(text, needle):
    """Every offset of `needle`, in order."""
    at = text.find(needle)
    while at >= 0:
        yield at
        at = text.find(needle, at + 1)

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def _run(body: str):
    script = ("import { makeRoom } from './tests/client/concord_scroll_gesture_runtime.mjs';\n"
              + body)
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_flick_that_lands_during_a_restore_is_still_the_readers():
    """THE REPORT. The reader is at the bottom, a picture lands, and they flick up inside that
    frame. Their scroll decides `pinned`, so losing it loses the position for every growth after."""
    got = _run("""
      const r = makeRoom();
      r.seed({ pinned: true, top: 1400, height: 2000 });
      r.watch(); r.frame();
      r.grow(2400);                       // a picture finishes decrypting: the list gets taller
      const heard = r.drag(900);          // …and the finger moves inside that same frame
      const during = r.state().pinned;
      r.frame();
      r.grow(2800);                       // the next picture
      r.frame();
      console.log(JSON.stringify({ heard, during, top: r.box.scrollTop,
                                   pinned: r.state().pinned,
                                   atBottom: r.box.scrollTop >= r.box.scrollHeight - r.box.clientHeight - 80 }));
    """)
    assert got["heard"] == "seen", (
        "the reader's scroll was discarded because the app happened to be restoring in the same "
        "frame — this is the report")
    assert got["during"] is False, "the reader scrolled away from the bottom and is still 'pinned'"
    assert got["atBottom"] is False, (
        "the next growth snapped the reader back to the bottom: 'my position keeps getting reset "
        "when I scroll through a room history'")
    assert got["top"] == 900, got


def test_the_apps_own_restore_is_still_not_mistaken_for_the_reader():
    """The other half, and the reason the mark exists at all. If a programmatic move registered as a
    scroll, every restore would recompute `pinned` from where IT put the scroller — which is how a
    reader who is following a live room gets quietly unpinned and stops seeing new messages."""
    got = _run("""
      const r = makeRoom();
      r.seed({ pinned: false, top: 400, height: 2000 });
      r.watch(); r.frame();
      r.grow(2400);                       // restores the anchor, moving the scroller itself
      const echo = r.echo();              // the scroll event that move produces
      console.log(JSON.stringify({ echo, pinned: r.state().pinned }));
    """)
    assert got["echo"] == "swallowed", (
        "the app's own restore is being read as the reader scrolling")
    assert got["pinned"] is False


def test_a_reader_at_the_bottom_stays_pinned_through_growth():
    """Following a live room must keep working: no flick, so nothing unpins, and each new message
    brings the reader with it."""
    got = _run("""
      const r = makeRoom();
      r.seed({ pinned: true, top: 1400, height: 2000 });
      r.watch(); r.frame();
      for (const h of [2400, 2800, 3200]) { r.grow(h); r.frame(); }
      console.log(JSON.stringify({ top: r.box.scrollTop, height: r.box.scrollHeight,
                                   bottom: r.box.scrollHeight - r.box.clientHeight,
                                   pinned: r.state().pinned }));
    """)
    assert got["pinned"] is True, "a reader sitting at the bottom was unpinned by ordinary growth"
    assert got["top"] == got["bottom"], "new messages no longer bring the reader with them"


# ─────────────────── …and the broader report: "it constantly jerks me to different positions" ──────
#
# Stopping the snap to the bottom was only one of three ways this scroller moved under the reader.
# The other two are here: a correction written WHILE the hand is on the scroller (which cancels the
# fling, so the reader lands somewhere they did not choose), and a correction written when there is
# nothing to correct (assigning the value it already holds cancels momentum just the same).


def test_nothing_moves_the_scroller_while_the_hand_is_on_it():
    """THE REPORT, in its general form. A room full of decrypting pictures fires a resize every few
    hundred milliseconds; each one used to write `scrollTop`, and a write mid-fling stops the fling
    dead. The reader is mid-gesture — they are choosing the position, and nothing else may."""
    got = _run("""
      const r = makeRoom();
      r.seed({ pinned: false, top: 400, height: 2000 });
      r.box.scrollTop = 400;
      r.watch(); r.frame();
      r.hand('touchstart');               // a finger goes down and starts a flick
      r.box.scrollTop = 260;
      r.grow(3000);                       // a picture lands above, mid-gesture
      r.frame();
      const during = r.box.scrollTop;
      r.wait(400);                        // the hand leaves and the momentum ends
      console.log(JSON.stringify({ during, after: r.box.scrollTop }));
    """)
    assert got["during"] == 260, (
        "the scroller was moved while the reader was mid-flick — the fling dies there and they land "
        "somewhere they did not choose")


def test_a_correction_that_changes_nothing_is_not_written_at_all():
    """A no-op write is not free: assigning the value it already holds still cancels a momentum
    scroll. So 'nothing to correct' has to mean writing NOTHING — which is why this counts writes
    rather than reading the position afterwards. Reading the position cannot tell the two apart,
    and an earlier version of this test could not either."""
    got = _run("""
      const r = makeRoom({ rows: 30, height: 3000, viewport: 600, top: 400 });
      r.seed({ pinned: false, top: 400, height: 3000 });
      r.box.scrollTop = 400;
      r.watch(); r.frame();
      const before = r.writes();
      r.grow(3600);                       // growth BELOW the reader: nothing they can see moved
      r.frame();
      console.log(JSON.stringify({ top: r.box.scrollTop, wrote: r.writes() - before }));
    """)
    assert got["top"] == 400, "growth below an unpinned reader moved them"
    assert got["wrote"] == 0, (
        "the scroller was written to %d time(s) though it was already in the right place — each of "
        "those cancels a momentum scroll for no gain" % got["wrote"])


def test_the_correction_still_happens_once_the_hand_has_gone():
    """Deferring is not dropping. A picture landing ABOVE the reader really does move the message
    they are looking at, and that has to be put back — just not while they are touching the
    scroller. Measured against real rows: the content gains 400px above, so the anchor row is 400px
    lower, and the restore follows it exactly."""
    got = _run("""
      const r = makeRoom({ rows: 30, height: 3000, viewport: 600, top: 400 });
      r.seed({ pinned: false, top: 400, height: 3000 });
      r.box.scrollTop = 400;
      r.watch(); r.frame();
      r.hand('touchstart');               // the reader's finger is down
      r.growAbove(400);                   // a picture above them finishes decrypting
      r.frame();
      const during = r.box.scrollTop;
      r.wait(600);                        // the hand leaves; the deferred correction gets its turn
      console.log(JSON.stringify({ during, after: r.box.scrollTop }));
    """)
    assert got["during"] == 400, "it moved the scroller while the reader was touching it"
    assert got["after"] == 800, (
        "the message the reader was looking at was not put back after the growth above it — %r"
        % (got,))


def test_the_room_scroller_has_exactly_one_owner_of_its_position():
    """THE THIRD CAUSE, and the one that made this read as random.

    A browser anchors a scroller itself when content grows above the viewport, and this room ALSO
    restores the reader's anchor from a ResizeObserver — two corrections for one decrypting picture,
    in an order nothing guarantees. `.feed` has set `overflow-anchor:none` for exactly this reason
    since the live-prepend was written, with the reason in a comment above it; `.cc-messages` never
    did, and it is the scroller people actually read history in.

    The JS is the owner because it is the half that also has to survive a full repaint — where every
    row is replaced and the browser has nothing left to anchor to — and iOS, which does not
    implement overflow-anchor at all."""
    css = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")
    # THE RULE THAT MAKES IT A SCROLLER, not the first one that mentions the class — several
    # `.cc-messages{...}` blocks exist (overflow-x clipping, the phone padding override) and the
    # first of them says nothing about scrolling.
    rule = next((css[i:css.index("}", i) + 1]
                 for i in (m for m in _all(css, ".cc-messages{"))
                 if "overflow-y:auto" in css[i:css.index("}", i) + 1]), None)
    assert rule, "re-point this test: no .cc-messages rule makes it a scroller any more"
    assert "overflow-anchor:none" in rule, (
        "the browser's scroll anchoring is back on the room scroller, so it and the ResizeObserver "
        "both correct the same growth — 'it constantly jerks me to different positions'")


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_live_message_does_not_repaint_the_room_under_a_moving_finger():
    """THE MOST FREQUENT JERK OF ALL, and the one no scroll-restore can fix.

    `preserveChatScroll` REPLACES the rows, and replacing the content of a scroller kills a momentum
    scroll outright. The restore afterwards puts the right pixel back, but the fling that was going
    to carry the reader another few hundred pixels is gone — so in a busy room, scrolling back
    through history is a series of dead flings, one per arriving message.

    The paint waits for the hand; the SAVE never does (that rule is
    `test_concord_background_notifications.py`). And five messages arriving during one flick are
    still one repaint, or the wait would just move the stutter to the end of the gesture."""
    got = _run("""
      const r = makeRoom({ rows: 10 });
      r.watch(); r.frame();
      let painted = 0;
      r.hand('touchstart');                       // the reader is mid-flick
      for (let i = 0; i < 5; i++) r.repaint(() => painted++);   // five messages land
      const during = painted;
      r.wait(600);                                 // the hand leaves
      const after = painted;
      r.repaint(() => painted++);                  // and with no hand on it, nothing waits
      console.log(JSON.stringify({ during, after, idle: painted }));
    """)
    assert got["during"] == 0, (
        "the room repainted under a moving finger — the fling dies there, which is what 'it "
        "constantly jerks me to different positions' feels like")
    assert got["after"] == 1, (
        "five messages arriving during one flick produced %d repaints; they must coalesce into one "
        "or the wait only moves the stutter to the end of the gesture" % got["after"])
    assert got["idle"] == 2, "a repaint with no hand on the scroller was delayed for no reason"
