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


def test_style_switch_never_changes_window_focus_or_identity():
    apply = function("applyDesktopStyle")
    for forbidden in ("focusWin", "handoff", "style.", "render", "openApp", "closeWin"):
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


def test_mac_style_is_a_desktop_experience_not_only_traffic_lights():
    mac = CSS[CSS.index("/* Optional macOS desktop experience"):
              CSS.index("@media(max-width:1180px)", CSS.index("/* Optional macOS desktop experience"))]
    for surface in (".os-root.os-style-mac::before", ".os-root.os-style-mac .os-mac-menu",
                    ".os-root.os-style-mac .os-bar", ".os-root.os-style-mac .os-tray",
                    ".os-root.os-style-mac .os-startmenu", ".os-root.os-style-mac .os-desk",
                    ".os-root.os-style-mac .os-icon", ".os-root.os-style-mac .osw-body"):
        assert surface in mac, surface + " is missing from the desktop theme"
    assert "backdrop-filter:blur(24px)" in mac
    assert "margin-inline:auto" in mac and "transform:none" in mac
    assert "position:fixed" in mac, "the tray was not moved into the top menu bar"
    assert "floating Dock" in mac


def test_mac_menu_is_accessible_real_controls_wired_to_existing_actions():
    enter = JS[JS.index("function enter()") : JS.index("function exit(")]
    assert 'aria-label="Desktop menu"' in enter
    for key in ("posterchan", "settings", "tasks", "view"):
        assert f'data-mac-menu="{key}"' in enter
    wire = function("wireMacMenu")
    for action in ("toggleStart()", "openSystemSettings()", "openTaskManager()", "toggleFull()"):
        assert action in wire
    assert "pointer-events:none" not in wire


def test_mac_menu_dock_and_widgets_use_separate_safe_rectangles():
    mac = CSS[CSS.index("/* Optional macOS desktop experience"):
              CSS.index("@media(max-width:1180px)", CSS.index("/* Optional macOS desktop experience"))]
    assert ".os-root.os-style-mac .os-desk{margin-top:var(--mac-menu-h);margin-bottom:calc(var(--mac-dock-h) + 18px)}" in mac
    assert ".os-root.os-style-mac .os-tray{position:fixed" in mac
    assert ".os-root.os-style-mac .os-bar{position:absolute" in mac
    assert "inset:0 min(430px,42vw) auto 7px" in mac


def test_mac_dock_does_not_capture_or_duplicate_the_top_tray():
    mac = CSS[CSS.index("/* Optional macOS desktop experience"):
              CSS.index("@media(max-width:1180px)", CSS.index("/* Optional macOS desktop experience"))]
    dock = mac[mac.index(".os-root.os-style-mac .os-bar{"):
               mac.index(".os-root.os-style-mac .os-start{")]
    assert "backdrop-filter:none" in dock
    assert ".os-root.os-style-mac .os-qbox{display:none}" in dock
    tray = mac[mac.index(".os-root.os-style-mac .os-tray{"):
               mac.index(".os-root.os-style-mac .os-clock")]
    assert "position:fixed" in tray
    assert "inset:0 9px auto auto" in tray


def test_style_change_reflows_windows_and_widgets_to_the_new_work_area():
    apply = function("applyDesktopStyle")
    for marker in ("requestAnimationFrame", "snapTo(w", "keepFrameReachable(w)", "drawWidgets()"):
        assert marker in apply


def test_style_is_device_global_and_not_copied_during_monitor_handoff():
    payload = JS[JS.index("function handoffPayload") : JS.index("function sendFrameHandoff")]
    assert "STYLE_KEY" not in payload and "osDesktopStyle" not in payload
    assert "applyDesktopStyle()" in JS
