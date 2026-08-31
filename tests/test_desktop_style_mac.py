from pathlib import Path
import subprocess


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
    for key in ("settings", "system-settings", "tasks", "view"):
        assert f'data-mac-menu="{key}"' in enter
    assert 'class="os-mac-appmenu"' in enter
    assert enter.count('>System Settings</button>') == 1
    menu_css = CSS[CSS.index(".os-root.os-style-mac .os-mac-menu{"):
                   CSS.index(".os-root.os-style-mac .os-mac-menu button{")]
    assert "overflow:visible" in menu_css
    wire = function("wireMacMenu")
    for action in ("openLauncherApp('settings')", "openSystemSettings()", "openTaskManager()", "toggleFull()"):
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
    place = function("placeDesktopTray")
    assert "root.classList.contains('os-style-mac')?root:bar" in place
    assert "host.appendChild(tray)" in place
    draw = function("drawBar")
    assert "placeDesktopTray();" in draw


def test_mac_mode_has_theme_specific_dock_and_status_icons():
    mac = CSS[CSS.index("/* Optional macOS desktop experience"):
              CSS.index("@media(max-width:1180px)", CSS.index("/* Optional macOS desktop experience"))]
    assert ".os-root.os-style-mac .os-task>.os-app-ic" in mac
    assert ".os-root.os-style-mac .os-task>.ic" in mac
    # The Dock tiles are macOS app artwork now: a squircle carrying the APP's own hue, rather than
    # the old fixed 145deg gradients cycled by :nth-child position (see
    # tests/client/test_mac_app_tint.py — the same app changed colour when a neighbour opened).
    assert "border-radius:23%" in mac, "the Dock tiles lost their squircle"
    assert "hsl(var(--app-h" in mac, "the Dock tiles no longer take the app's own colour"
    assert ".os-root.os-style-mac .os-tray-group" in mac
    assert "compact monochrome symbols" in mac


def test_tray_reparenting_executes_in_both_desktop_styles():
    run = subprocess.run(["node", "tests/client/mac_tray_sim.js"], cwd=ROOT,
                         text=True, capture_output=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "structurally separate" in run.stdout


def test_style_change_reflows_windows_and_widgets_to_the_new_work_area():
    apply = function("applyDesktopStyle")
    for marker in ("requestAnimationFrame", "snapTo(w", "keepFrameReachable(w)", "drawWidgets()"):
        assert marker in apply


def test_style_is_device_global_and_not_copied_during_monitor_handoff():
    payload = JS[JS.index("function handoffPayload") : JS.index("function sendFrameHandoff")]
    assert "STYLE_KEY" not in payload and "osDesktopStyle" not in payload
    assert "applyDesktopStyle()" in JS
