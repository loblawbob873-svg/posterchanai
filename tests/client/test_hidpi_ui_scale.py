"""A 3840x2560 monitor at output scale 1 must not be drawn at 1921px size.

THE OUTPUT SCALE IS NOT AVAILABLE AS A FIX, and that is the whole reason this tier exists. Running
the panel at compositor scale 1.25 makes everything readable in one line and was measured to cost
every game dearly: Xwayland gets no `-scale`, so a fullscreen game renders 3072x2048 and is upscaled
to the panel — blurry, and with the buffer never matching the output mode DIRECT SCANOUT is
impossible and every frame pays a composite+scale pass. So the outputs stay at 1 and the UI scales.

The client's `body{zoom}` tiers only ever scaled DOWN (.77/.72/.67 below 1920) and stopped at
`zoom:1` above 1921px, so there was no tier at all for a screen that is four times a 1080p one.

What is checked here is the STYLESHEET and the shipped os.js, as text and under node — a real
browser measures the result in scripts/check_hidpi_ui_scale.py, which is the other half.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
# Comments quote every one of these numbers as prose; strip them before matching rules.
CODE = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

TIER = r"@media \(min-width:(\d+)px\) and \(min-height:(\d+)px\)\s*\{([^}]*\{[^}]*\}[^}]*)+?\}"


def _tier_block() -> str:
    """The 4K-class tier, by its media query, brace-matched."""
    i = CODE.find("@media (min-width:3500px) and (min-height:1800px)")
    assert i >= 0, (
        "there is no zoom tier for a 4K-class panel. Above 1921px the client stops at zoom 1, so a "
        "3840x2560 monitor is drawn at exactly the size a 1921px one is — at four times the pixels."
    )
    depth, j = 0, CODE.index("{", i)
    for k in range(j, len(CODE)):
        if CODE[k] == "{":
            depth += 1
        elif CODE[k] == "}":
            depth -= 1
            if depth == 0:
                return CODE[i:k + 1]
    raise AssertionError("unbalanced braces in the 4K tier")


def test_a_4k_class_panel_gets_a_tier_that_scales_up():
    block = _tier_block()
    m = re.search(r"zoom\s*:\s*var\(\s*--ui-scale\s*,\s*([0-9.]+)\s*\)", block)
    assert m, "the 4K tier does not set `zoom` from var(--ui-scale, N): %r" % block
    assert float(m.group(1)) > 1, (
        "the 4K tier's default zoom is %s — it is meant to scale the app UP" % m.group(1))


def test_zoom_zf_and_the_app_height_are_the_same_number_in_that_tier():
    """The recurring failure: a rule that moves `zoom` and forgets one of the other two.

    `--zf` exists purely to undo the zoom for viewport-unit containers, and `.app` is the container
    whose top went off screen on Windows when they disagreed."""
    block = _tier_block()
    zoom = re.search(r"zoom\s*:\s*var\(\s*--ui-scale\s*,\s*([0-9.]+)\s*\)", block)
    zf = re.search(r"--zf\s*:\s*var\(\s*--ui-scale\s*,\s*([0-9.]+)\s*\)", block)
    app = re.search(r"\.app\{\s*height:calc\(100dvh / var\(\s*--ui-scale\s*,\s*([0-9.]+)\s*\)\)\s*\}",
                    block.replace("\n", "").replace("  ", ""))
    assert zf, "the 4K tier sets zoom and no --zf: %r" % block
    assert app, "the 4K tier sets zoom and does not pair `.app{height:calc(100dvh / …)}`: %r" % block
    assert zoom.group(1) == zf.group(1) == app.group(1), (
        "zoom / --zf / .app-height disagree in the 4K tier: %s / %s / %s"
        % (zoom.group(1), zf.group(1), app.group(1)))


def test_the_tier_is_gated_on_height_as_well_as_width():
    """Width alone cannot tell a 4K panel from a WIDE one.

    A 34" 3440x1440 and a 49" 5120x1440 are ~110ppi ordinary desktops that must keep zoom 1, and
    both are wider than any threshold that still catches 3840. Their HEIGHT is what separates them.
    A width-only tier would blow both of them up by 25%."""
    block = _tier_block()
    w = int(re.search(r"min-width:(\d+)px", block).group(1))
    h = int(re.search(r"min-height:(\d+)px", block).group(1))
    assert w > 3440, "min-width %d also catches a 3440x1440 ultrawide" % w
    assert w <= 3840, "min-width %d does not catch a 3840-wide panel" % w
    assert 1441 <= h <= 2160, (
        "min-height %d either lets a 1440-tall ultrawide in or excludes a 3840x2160 panel" % h)


def test_the_tier_wins_over_the_flat_native_scale_rules():
    """Cascade order, not specificity: these rules are all one class deep, so the LAST one that
    matches is the one that applies. A 3840px screen matches `min-width:1921px` too."""
    tier = CODE.index("@media (min-width:3500px) and (min-height:1800px)")
    for earlier in ("@media (min-width:1921px)",
                    "@media (min-width:1400px) and (min-resolution:1.2dppx)"):
        assert CODE.index(earlier) < tier, (
            "%s comes after the 4K tier, so it overrides it and a 4K panel is back at zoom 1"
            % earlier)


def test_every_tier_that_is_user_overridable_reads_one_variable():
    """One name, or a stored scale moves some rules and not others.

    The value is read three times per tier (zoom, --zf, .app height) and by every tier that is
    meant to be adjustable; a second variable name anywhere means a screen that matches two rules
    is drawn at one scale and measured at another."""
    tiers = re.findall(r"body\{[^}]*zoom[^}]*\}", CODE)
    assert tiers, "the zoom tiers moved — re-read this test"
    names = set()
    for t in tiers:
        names |= set(re.findall(r"var\(\s*(--[a-z0-9-]+)", t))
    assert names <= {"--ui-scale"}, (
        "a zoom tier reads a variable other than --ui-scale, so a stored scale moves some rules "
        "and not others: %s" % sorted(names))
    assert "--ui-scale" in names, "no zoom tier is user-overridable at all"


def test_nothing_below_the_tier_changed_shape():
    """The shrink tiers are what every laptop and tablet gets. This change must be invisible there."""
    for q, z in (("(min-width:821px) and (max-width:1920px)", ".77"),
                 ("(min-width:821px) and (max-width:1600px)", ".72"),
                 ("(min-width:821px) and (max-width:1366px)", ".67")):
        assert ("@media %s{ body{ zoom:%s; --zf:%s } .app{ height:calc(100dvh / %s) } "
                ".main{ height:100%% } }" % (q, z, z, z)) in CODE, \
            "the %s shrink tier moved" % q


# ---- the client's own control -------------------------------------------------------------------

def _node(program: str):
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def _fn(name: str) -> str:
    i = OS_JS.index("function %s(" % name)
    depth, j = 0, OS_JS.index("{", i)
    for k in range(j, len(OS_JS)):
        if OS_JS[k] == "{":
            depth += 1
        elif OS_JS[k] == "}":
            depth -= 1
            if depth == 0:
                return OS_JS[i:k + 1]
    raise AssertionError(name)


@pytest.mark.parametrize("stored,expect_set", [(None, False), ("", False), ("nonsense", False),
                                               (7, False), (0.1, False), (1.5, True)])
def test_the_applier_removes_the_property_rather_than_writing_a_1(stored, expect_set):
    """An ABSENT choice must leave the tier alone.

    Writing `--ui-scale: 1` when nothing is stored would pin every screen to 1 and silently delete
    the 4K default — the app would look exactly as it did before the tier existed, with the tier
    present in the stylesheet and doing nothing, which is the hardest kind of bug to see.
    A stored value out of range is the same case: it is not a choice, so it must not be applied."""
    program = """
      const UI_SCALE_KEY = 'osUiScale';
      const settings = () => ({ get: (k, d) => (%(stored)s === null ? d : %(stored)s), set(){} });
      const calls = [];
      const document = { documentElement: { style: {
        setProperty: (k, v) => calls.push(['set', k, v]),
        removeProperty: k => calls.push(['remove', k]) } } };
      %(uiScaleStored)s
      %(applyUiScale)s
      applyUiScale();
      process.stdout.write(JSON.stringify(calls));
    """ % {"stored": json.dumps(stored), "uiScaleStored": _fn("uiScaleStored"),
           "applyUiScale": _fn("applyUiScale")}
    calls = _node(program)
    assert calls, "applyUiScale did nothing at all"
    if expect_set:
        assert calls == [["set", "--ui-scale", str(stored)]], calls
    else:
        assert calls == [["remove", "--ui-scale"]], (
            "a %r choice was applied as a scale instead of being left to the stylesheet: %s"
            % (stored, calls))


def test_the_settings_control_shows_what_is_actually_drawn():
    """With nothing stored the effective scale is the TIER's default, not 1.

    A select that pre-selects 100% on a screen being drawn at 125% makes the one honest choice look
    like a change and makes choosing 125% look like a no-op."""
    program = """
      const UI_SCALE_KEY = 'osUiScale';
      const settings = () => ({ get: (k, d) => d, set(){} });
      const window = { matchMedia: q => ({ matches: %(big)s }) };
      const matchMedia = window.matchMedia;
      %(stored)s
      const UI_SCALE_BIG = %(query)s;
      %(eff)s
      process.stdout.write(JSON.stringify(uiScaleEffective()));
    """
    q = json.dumps(re.search(r"UI_SCALE_BIG = '([^']+)'", OS_JS).group(1))
    big = _node(program % {"big": "true", "stored": _fn("uiScaleStored"),
                           "eff": _fn("uiScaleEffective"), "query": q})
    small = _node(program % {"big": "false", "stored": _fn("uiScaleStored"),
                             "eff": _fn("uiScaleEffective"), "query": q})
    assert big > 1, "on a 4K-class panel the control would pre-select %s" % big
    assert small == 1, "on an ordinary desktop the control would pre-select %s" % small


def test_the_control_and_the_stylesheet_agree_on_which_screens_are_big():
    """The @media line and the matchMedia string are the same fact written twice — there is no way
    to ask a stylesheet which rule won — so they are compared here rather than left to drift."""
    q = re.search(r"UI_SCALE_BIG = '([^']+)'", OS_JS).group(1)
    assert "@media " + q in CODE, (
        "os.js decides a screen is 4K-class with %r and the stylesheet uses a different query" % q)
    default = float(re.search(r"zoom\s*:\s*var\(\s*--ui-scale\s*,\s*([0-9.]+)\s*\)",
                              _tier_block()).group(1))
    js_default = float(re.search(r"matchMedia\(UI_SCALE_BIG\)\.matches\) return ([0-9.]+)",
                                 OS_JS).group(1))
    assert default == js_default, (
        "the stylesheet defaults a 4K panel to %s and the settings control says %s"
        % (default, js_default))


def test_there_is_a_control_and_it_is_wired():
    """A number in a stylesheet is a number nobody can adjust, and the owner had already changed his
    mind about this once. Both halves are asserted: the select, and an onchange that stores it."""
    assert "data-ui-scale" in OS_JS, "Settings → Appearance has no display-scale control"
    assert re.search(r"\[data-ui-scale\]'\);if\(uiScale\)uiScale\.onchange", OS_JS), \
        "the display-scale control is drawn and nothing is bound to it"
    assert "setUiScale(uiScale.value)" in OS_JS
    assert "UI_SCALE_CHOICES" in OS_JS


def test_changing_the_scale_tells_the_desktop_to_remeasure():
    """`body{zoom}` changing fires no resize event, and the desktop caches its work area, icon grid
    and window placement in LAYOUT pixels taken at the old zoom."""
    i = OS_JS.index("[data-ui-scale]")
    handler = OS_JS[i:i + 900]
    assert "new Event('resize')" in handler, (
        "nothing tells the desktop the screen effectively changed size, so the icon grid and every "
        "window opened next are laid out against a viewport that no longer exists")
