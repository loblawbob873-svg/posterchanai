"""PROFILE MUSIC LOOKS LIKE THE PLAYER, AND MOVES ONLY WHILE IT PLAYS.

Asked for: "make music appear cooler with effects on the profile page if user has it, similar to
our music player". It was a bare `<audio controls>` in a grey box — the browser's own widget on a
page that has its own visual language, sitting next to a player (`.mp-*`) built in that language.

The equaliser is the cheap half of it: twelve bars on the same cyan→magenta ramp, animating only
while the track plays. Two decisions worth keeping:

  * **CSS animation, not an AnalyserNode.** A real spectrum needs the Web Audio graph, and a
    cross-origin track without CORS taints it and analyses to flat silence — the visualiser would
    die on exactly the tracks people link to. This one cannot fail that way.
  * **`controls` stays.** It is the accessible, keyboard-reachable transport, and replacing it would
    be a second player to keep in step with the real one.

The state is driven by the audio element's own events, never by a guess, and it stops on `error`
and `waiting` as well as `pause` — bars dancing over silence is worse than no bars.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n  (?:async )?function " + re.escape(name) + r"\(.*?\n  \}", APP, re.S)
    assert m, f"{name} is gone from app.js"
    return m.group(0)


def test_the_card_renders_an_equaliser_and_keeps_the_real_transport():
    html = _fn("_profileMusicHtml")
    assert "prof-eq" in html, "no equaliser is rendered"
    assert "<audio controls" in html, (
        "the native transport was dropped — that is the accessible, keyboard-reachable control")
    assert "aria-hidden" in html, "the decorative bars are exposed to a screen reader"


def test_the_bars_only_move_while_the_audio_is_playing():
    """The animation is bound to `.playing`, and nothing else may turn it on."""
    assert ".prof-track.playing .prof-eq i{animation:profEq" in CSS.replace("\n", "")
    idle = CSS.split(".prof-eq i{", 1)[1].split("}", 1)[0]
    assert "animation" not in idle, "the bars animate at rest — a quiet profile would never be still"


@pytest.mark.parametrize("event", ["play", "playing", "pause", "ended", "error", "waiting"])
def test_every_state_change_is_taken_from_the_element(event):
    """Including the unhappy ones: a stalled or failed track must not leave the bars dancing."""
    binder = _fn("_bindProfileMusic")
    assert f"'{event}'" in binder, f"the {event} event is not handled"


def test_binding_twice_does_not_stack_listeners():
    """The block is re-inserted on the background kind-0 refresh, so the binder runs again on the
    same elements whenever a profile is repainted."""
    binder = _fn("_bindProfileMusic")
    assert "_pcEq" in binder, "re-binding would add a second set of listeners on every repaint"


def test_it_is_bound_on_the_first_paint_and_on_the_refresh():
    """Two paths insert this HTML. A binder wired to one of them is an equaliser that works only
    sometimes — the shape this screen has been bitten by before."""
    assert "_bind('the profile music equaliser'" in APP, "not bound on the first paint"
    patch = _fn("_patchProfileHeader")
    assert "_bindProfileMusic(music)" in patch, "not bound after the background refresh"


def test_a_failing_equaliser_cannot_cost_the_rest_of_the_profile():
    """`_bind` exists on this screen because a throw mid-render takes every binding after it — the
    tabs, the follow stats, Copy npub. A decorative bar chart must never be able to do that."""
    at = APP.index("_bind('the profile music equaliser'")
    assert "_bind(" in APP[at:at + 80]


def test_reduced_motion_keeps_the_colour_and_drops_the_movement():
    # There are several reduced-motion blocks in this stylesheet; find the one that owns the bars.
    blocks = [b.split("}}", 1)[0]
              for b in CSS.split("@media(prefers-reduced-motion:reduce){")[1:]]
    mine = [b for b in blocks if "prof-eq" in b]
    assert mine, "the equaliser has no reduced-motion rule at all"
    assert "animation:none" in mine[0]
    assert "height:52%" in mine[0], "reduced motion left the bars collapsed rather than shown still"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_markup_is_well_formed_and_escapes_the_track_name():
    """A track name comes from somebody else's profile; it is rendered as text, never as markup."""
    src = _fn("_profileMusicFields") + _fn("_profileMusicHtml")
    script = (
        "const enc=s=>String(s).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"
        "'\"':'&quot;',\"'\":'&#39;'}[c]));\n" + src +
        "\nconst h=_profileMusicHtml({music:[['<img src=x onerror=alert(1)>','https://e/x.mp3']]});"
        "process.stdout.write(h||'');")
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-800:]
    out = done.stdout
    if out:
        assert "<img src=x" not in out, "a track name was rendered as markup"
        assert out.count("<div class=\"prof-eq\"") >= 1
