from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_effect_mode_is_persisted_and_applied_on_every_desktop_entry():
    assert "const FX_KEY = 'osCompositing'" in JS
    start = JS.index("function applyDesktopEffects")
    apply = JS[start:JS.index("\n  }", start) + 4]
    assert "desktopEffectsMode()" in apply
    assert "classList.toggle('os-fx'" in apply and "classList.toggle('os-fx-off'" in apply
    enter = JS[JS.index("function enter()") : JS.index("function exit(")]
    assert enter.index("root.className = 'os-root'") < enter.index("applyDesktopEffects()")


def test_low_power_off_removes_every_added_effect():
    assert 'data-window-effects' in JS and 'Low power / off' in JS
    off = CSS[CSS.index(".os-root.os-fx-off .osw"):
              CSS.index("@media(prefers-reduced-motion:reduce)", CSS.index(".os-root.os-fx-off .osw"))]
    for rule in ("box-shadow:none", "backdrop-filter:none", "transition:none"):
        assert rule in off


def test_compositing_is_presentation_only_and_cannot_break_window_state_or_input():
    start = JS.index("function applyDesktopEffects")
    apply = JS[start:JS.index("\n  }", start) + 4]
    for forbidden in ("focusWin", "nsync", "handoff", "style.", "render", "openApp", "closeWin"):
        assert forbidden not in apply
    fx = CSS[CSS.index(".os-root.os-fx .osw:not"):
             CSS.index("@media(prefers-reduced-motion:reduce)", CSS.index(".os-root.os-fx .osw:not"))]
    for forbidden in ("z-index", "pointer-events", "visibility", "opacity", "transform"):
        assert forbidden not in fx
    assert ".native-stashed" not in fx and ".native-fullscreen-frame" not in fx


def test_effect_choice_is_global_not_part_of_cross_monitor_window_identity():
    payload = JS[JS.index("function handoffPayload") : JS.index("function sendFrameHandoff")]
    assert "osCompositing" not in payload and "FX_KEY" not in payload
    assert "applyDesktopEffects()" in JS


def test_dragging_temporarily_disables_full_window_blur_and_transitions():
    start = CSS.index(".osw.dragging{")
    rule = CSS[start:CSS.index("}", start) + 1]
    assert "will-change:transform" in rule
    assert "transition:none!important" in rule
    assert "backdrop-filter:none!important" in rule


def test_automatic_effects_follow_input_capability_and_preserve_explicit_choices():
    start = JS.index("function desktopEffectsMode()")
    end = JS.index("function applyDesktopStyle()", start)
    script = """
const assert = require('node:assert/strict');
const FX_KEY = 'osCompositing';
let stored, coarse = false;
const settings = () => ({get: (key, fallback) => stored ?? fallback});
const window = {matchMedia: query => {
  assert.equal(query, '(any-pointer: coarse)');
  return {matches: coarse};
}};
const classes = new Set();
const root = {classList: {toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name)}};
""" + JS[start:end] + """
for (const [value, touch, expected] of [
  [undefined, true, false], [undefined, false, true],
  ['auto', true, false], ['auto', false, true],
  ['full', true, true], ['off', false, false], ['invalid', true, false]
]) {
  stored = value; coarse = touch; applyDesktopEffects();
  assert.equal(classes.has('os-fx'), expected);
  assert.equal(classes.has('os-fx-off'), !expected);
  assert.equal(stored, value); // Device defaults never overwrite a saved preference.
}
"""
    subprocess.run(['node', '-e', script], check=True)
