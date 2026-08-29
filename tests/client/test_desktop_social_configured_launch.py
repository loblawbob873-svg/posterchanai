"""Desktop Social launchers honor the configured landing timeline."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def test_client_exports_authoritative_social_timeline_resolver():
    bridge = APP[APP.index("window.__PC ="):]
    assert "socialTimeline: () => _startTimeline()" in bridge


def test_desktop_resolves_only_the_launcher_social_alias():
    helper = OS[OS.index("function openLauncherApp(view)"):]
    helper = helper[:helper.index("\n  /*", 10)]
    assert "view==='global' && PC().socialTimeline" in helper
    assert "target=PC().socialTimeline()||view" in helper
    assert "return openApp(target,app&&app.label,app&&app.icon)" in helper
    # Explicit route navigation must retain the literal Home/Nostrverse tab the user selected.
    route = OS[OS.index("function routeView(view, focusOnly)"):]
    route = route[:route.index("\n  /*", 10)]
    assert "openLauncherApp" not in route
    assert "return !!openApp(view)" in route


def test_every_desktop_launcher_entry_uses_the_resolver():
    assert "run: () => openLauncherApp(key)" in OS          # icon context menu
    assert "openLauncherApp(b.dataset.view);" in OS         # desktop icon and Start menu
    assert "openLauncherApp(b.dataset.pin.slice(5))" in OS  # taskbar pin
    assert "toggleStart(false); openLauncherApp(view)" in OS # Start context menu
    assert "run: () => openApp(key)" not in OS
