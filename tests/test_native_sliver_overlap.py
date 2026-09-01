"""A SLIVER IS NOT A COVER — and the placement pass has to survive arrangements, not just cases.

Reported as "Settings is now glitching my screen and telegram, sticking to that on desktop". A
Settings window whose edge lapped about 38px over Telegram's took the WHOLE of Telegram off the
screen and left a frozen screenshot of it. `stashPlan` parked a native app on `overlaps()` — one
shared pixel — and windows abut all day long.

Why the threshold is not zero, and why it is measured against the COVERING window: both arguments
are written out beside the rule in osnative.js. The short of it is that parking costs the entire
app however small the overlap, while not parking costs only the band; and asking "how much of
Telegram is covered" would leave a maximised Firefox on top of a small dialog opened inside it,
which is a bug this desktop has already paid for twice.

The arrangement in the first tests is REAL — read off the running PosterChanOS desktop with
`swaymsg -t get_tree` while both apps were on screen:

    TelegramDesktop  floating  711,326   1278x1681
    firefox-bin      floating    9,63    3054x1948

Those are the compositor's pixels. `stashPlan` is fed `_frameRect(w)`, i.e. getBoundingClientRect
on the HTML frame, so its inputs are the shell's ZOOM-INCLUDED CSS pixels and `scale` converts to
sway only later, in `mapRect`. The shapes are what these tests are about and they carry over
unchanged, but the space matters for anyone tuning `SLIVER`: 64 is 64 CSS px, which is why it keeps
meaning the same visual size on a scaled display instead of shrinking with the zoom.

The rest of the file is the part that would actually have caught tonight: every native-window bug
this week was found by looking at the machine, never by a test, and each lived in the INTERACTION
between windows rather than in one function's arithmetic. So the second half asserts properties
over whole arrangements — including randomised ones — instead of adding more single cases.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "static/js/client/osnative.js"
NODE = shutil.which("node") or shutil.which("nodejs")

pytestmark = pytest.mark.skipif(not NODE, reason="needs node")

#: Measured on the live desktop (192.168.0.102), not invented.
TELEGRAM = {"left": 711, "top": 326, "width": 1278, "height": 1681}
FIREFOX = {"left": 9, "top": 63, "width": 3054, "height": 1948}


def run(body: str):
    src = (f"const N = require({json.dumps(str(MOD))});\nconst out = {{}};\n{body}\n"
           "process.stdout.write(JSON.stringify(out));")
    done = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout)


def plan(natives, windows):
    """Drive the SHIPPED stashPlan. natives/windows are (z, rect) pairs."""
    items = [{"native": i, "z": z, "minimised": False, "rect": r}
             for i, (z, r) in enumerate(natives)]
    wins = [{"z": z, "minimised": False, "rect": r} for z, r in windows]
    return run(f"out.p = N.stashPlan({json.dumps(items)}, {json.dumps(wins)});")["p"]


def parked(natives, windows):
    return set(plan(natives, windows)["stash"])


# --------------------------------------------------------------------------- the reported bug

def test_a_window_lapping_telegrams_edge_does_not_take_telegram_off_the_screen():
    """THE REPORT, with the real rectangle. Settings' edge over 38px of Telegram."""
    settings = {"left": TELEGRAM["left"] + TELEGRAM["width"] - 38, "top": 400,
                "width": 1200, "height": 900}
    assert parked([(5, TELEGRAM)], [(9, settings)]) == set(), (
        "a 38px lap still parks the whole of Telegram — this is the frozen-screenshot report")


def test_a_dialog_inside_a_maximised_firefox_still_parks_it():
    """THE OPPOSITE ERROR, and the reason the threshold is not measured against the native window.
    A 400x300 dialog covers about 2% of a maximised Firefox. Judged that way Firefox would stay on
    top — floating over tiled — and the dialog would be invisible AND unclickable."""
    dialog = {"left": 1200, "top": 700, "width": 400, "height": 300}
    assert parked([(5, FIREFOX)], [(9, dialog)]) == {0}


def test_working_over_telegram_still_parks_it():
    """The threshold must not have turned into "never park", which is the lazy way to pass the
    first test and puts Telegram permanently on top of everything."""
    over = {"left": 900, "top": 500, "width": 900, "height": 900}
    assert parked([(5, TELEGRAM)], [(9, over)]) == {0}


def test_windows_that_merely_touch_are_not_covering_anything():
    abut = {"left": TELEGRAM["left"] + TELEGRAM["width"], "top": TELEGRAM["top"],
            "width": 600, "height": 900}
    assert parked([(5, TELEGRAM)], [(9, abut)]) == set()


def test_a_narrow_palette_can_still_park_what_it_sits_on_top_of():
    """The exception in the rule, stated as a test. A window NARROWER than the slop can never
    overlap by more than its own width, so judging it against a fixed 64px would make a 40px-wide
    tool palette unable to park anything it was deliberately placed over."""
    palette = {"left": 900, "top": 500, "width": 40, "height": 900}
    assert parked([(5, TELEGRAM)], [(9, palette)]) == {0}


# --------------------------------------------------------------------------- arrangement rules

def test_a_window_underneath_never_parks_the_app_above_it():
    """Direction of the z comparison, driven rather than grepped: a PosterChan window BELOW a
    native app must not touch it, or every window parks everything it shares pixels with."""
    over = {"left": 900, "top": 500, "width": 900, "height": 900}
    assert parked([(20, TELEGRAM)], [(3, over)]) == set()


def test_a_minimised_app_is_parked_however_the_windows_sit():
    assert parked([(5, TELEGRAM)], []) == set()
    got = run("out.p = N.stashPlan([{native:1,z:5,minimised:true,rect:"
              + json.dumps(TELEGRAM) + "}], []);")["p"]
    assert got["stash"] == [1] and got["show"] == []


def test_a_minimised_posterchan_window_covers_nothing():
    """A window that is not on the screen cannot be the reason an app leaves it."""
    over = {"left": 900, "top": 500, "width": 900, "height": 900}
    got = run("out.p = N.stashPlan("
              "[{native:1,z:5,minimised:false,rect:" + json.dumps(TELEGRAM) + "}],"
              "[{z:9,minimised:true,rect:" + json.dumps(over) + "}]);")["p"]
    assert got["stash"] == [], "a minimised window still parks the app underneath it"


def test_an_app_with_no_measurable_rectangle_is_parked_not_placed():
    """Placing a surface with a zero rectangle is worse than parking it — it is the empty hole."""
    for bad in ({"left": 0, "top": 0, "width": 0, "height": 500}, None):
        got = run("out.p = N.stashPlan([{native:1,z:5,minimised:false,rect:"
                  + json.dumps(bad) + "}], []);")["p"]
        assert got["stash"] == [1], f"a {bad} rectangle was placed rather than parked"


# --------------------------------------------------------------------------- whole arrangements

ARRANGEMENT = """
  function rnd(seed){ let s = seed >>> 0; return () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296; }
  const r = rnd(SEED);
  const rect = () => { const w = 40 + Math.floor(r()*1600), h = 40 + Math.floor(r()*1200);
    return {left: Math.floor(r()*2400), top: Math.floor(r()*1600), width: w, height: h}; };
  out.cases = [];
  for(let n = 0; n < 400; n++){
    const items = [], wins = [];
    for(let i = 0; i < 1 + Math.floor(r()*4); i++)
      items.push({native:i, z: Math.floor(r()*40), minimised: r() < 0.15, rect: rect()});
    for(let i = 0; i < Math.floor(r()*5); i++)
      wins.push({z: Math.floor(r()*40), minimised: r() < 0.15, rect: rect()});
    const p = N.stashPlan(items, wins);
    out.cases.push({items, wins, p, again: N.stashPlan(items, wins)});
  }
"""


@pytest.fixture(scope="module")
def arrangements():
    return run(ARRANGEMENT.replace("SEED", "20260901"))["cases"]


def test_every_app_gets_exactly_one_verdict(arrangements):
    """The invariant the whole placement pass rests on. An app in NEITHER list is never told what
    to do — it keeps whatever it was doing, which is how a surface ends up parked with a live frame
    over it (the empty hole) or shown under a frame that thinks it is parked."""
    for case in arrangements:
        ids = [it["native"] for it in case["items"]]
        verdicts = case["p"]["stash"] + case["p"]["show"]
        assert sorted(verdicts) == sorted(ids), (
            f"apps {sorted(set(ids) - set(verdicts))} got no verdict / "
            f"{sorted(set(verdicts) - set(ids))} were invented:\n{json.dumps(case, indent=1)}")
        assert not (set(case["p"]["stash"]) & set(case["p"]["show"])), (
            "an app was told to park AND to show in the same pass")


def test_the_same_arrangement_always_decides_the_same_way(arrangements):
    """Nothing here may depend on iteration order or hidden state. A plan that flickers between two
    answers is a surface that parks and unparks sixty times a second."""
    for case in arrangements:
        assert case["p"] == case["again"], json.dumps(case, indent=1)


def test_nothing_is_parked_without_a_window_that_actually_covers_it(arrangements):
    """The property the reported bug violated, stated over every arrangement rather than for one
    pair of rectangles: an app is only ever parked because it is minimised, because it has no
    rectangle, or because some window ABOVE it covers more than a sliver."""
    for case in arrangements:
        by_id = {it["native"]: it for it in case["items"]}
        for native in case["p"]["stash"]:
            it = by_id[native]
            rect = it["rect"]
            if it["minimised"] or not rect or not rect["width"] or not rect["height"]:
                continue
            covering = run(
                f"const it = {json.dumps(it)};\n"
                f"out.any = {json.dumps(case['wins'])}.some(w => w && !w.minimised "
                f"&& w.z > (it.z || 0) && N.coversMoreThanASliver(it.rect, w.rect));")["any"]
            assert covering, (
                "an app was parked with nothing above it covering it — on screen that is an app "
                f"that vanished for no reason:\n{json.dumps(case, indent=1)}")


def test_raising_a_window_never_brings_an_app_back_over_it():
    """Monotonicity, which is what makes clicking a window feel like anything. If raising the
    window you clicked could UN-park an app that covers it, the click would put the app in front —
    the exact complaint ("Telegram sits on top of whatever you click") in reverse."""
    over = {"left": 900, "top": 500, "width": 900, "height": 900}
    low = parked([(5, TELEGRAM)], [(3, over)])
    high = parked([(5, TELEGRAM)], [(9, over)])
    assert low == set() and high == {0}
    assert low <= high, "raising the window you clicked un-parked the app covering it"


def test_the_threshold_is_measured_in_the_space_the_shell_hands_it():
    """`SLIVER` is a number of pixels, so it only means anything if the space is known. The plan is
    fed `_frameRect`, which is getBoundingClientRect and therefore CSS pixels with the responsive
    body zoom already in them — the same space the windows are laid out in. Feeding it compositor
    pixels instead would silently rescale the threshold by the display factor: on a 2x output a
    sliver would become 32 CSS px and the reported bug would come part of the way back.

    There is a comment three lines above the call warning that mixing these two spaces "applies
    zoom twice and makes the real Wayland surface exactly half the HTML frame" — it has been got
    wrong here before, one field over."""
    os_js = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    call = os_js[os_js.index("const items = nativeWins().map"):]
    call = call[:call.index("const plan = NAT().stashPlan")]
    assert "rect: _frameRect(w)" in call, (
        "stashPlan is no longer fed frame rectangles — if these are now compositor pixels, SLIVER "
        "means a different size on every display and must be scaled at the call site")
    frame = os_js[os_js.index("function _frameRect(w)"):]
    assert "getBoundingClientRect()" in frame[:300]
