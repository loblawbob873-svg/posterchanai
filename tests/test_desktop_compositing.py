from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_effect_mode_is_persisted_and_applied_on_every_desktop_entry():
    assert "const FX_KEY = 'osCompositing'" in JS
    start = JS.index("function applyDesktopEffects")
    apply = JS[start:JS.index("\n  }", start) + 4]
    assert "settings().get(FX_KEY,'full')" in apply
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
