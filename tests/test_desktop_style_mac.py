from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def function(name):
    start = JS.index("function " + name)
    return JS[start:JS.index("\n  }", start) + 4]


def test_optional_style_is_persisted_restored_and_reversible():
    assert "const STYLE_KEY = 'osDesktopStyle'" in JS
    apply = function("applyDesktopStyle")
    assert "settings().get(STYLE_KEY,'posterchan')==='mac'" in apply
    assert "classList.toggle('os-style-mac'" in apply
    assert 'data-desktop-style' in JS
    assert "settings().set(STYLE_KEY,desktopStyle.value==='mac'?'mac':'posterchan')" in JS
    enter = JS[JS.index("function enter()") : JS.index("function exit(")]
    assert enter.index("root.className = 'os-root'") < enter.index("applyDesktopStyle()")


def test_style_switch_never_touches_window_focus_geometry_or_identity():
    apply = function("applyDesktopStyle")
    for forbidden in ("focusWin", "nsync", "handoff", "style.", "render", "openApp", "closeWin"):
        assert forbidden not in apply
    handler = JS[JS.index("const desktopStyle="):JS.index("const live=", JS.index("const desktopStyle="))]
    for forbidden in ("focusWin", "nsync", "drawDesktop", "drawBar", "renderSystemSettings"):
        assert forbidden not in handler


def test_mac_css_is_chrome_only_and_preserves_input_and_stacking():
    mac = CSS[CSS.index(".os-root.os-style-mac .osw"):
              CSS.index(".osw.focused", CSS.index(".os-root.os-style-mac .osw"))]
    for forbidden in ("z-index", "pointer-events", "visibility", "display:none", "position:",
                      "transform", "opacity", "width:calc", "height:calc"):
        assert forbidden not in mac
    assert ".native-stashed" not in mac and ".native-fullscreen-frame" not in mac


def test_style_is_device_global_and_not_copied_during_monitor_handoff():
    payload = JS[JS.index("function handoffPayload") : JS.index("function sendFrameHandoff")]
    assert "STYLE_KEY" not in payload and "osDesktopStyle" not in payload
    assert "applyDesktopStyle()" in JS
