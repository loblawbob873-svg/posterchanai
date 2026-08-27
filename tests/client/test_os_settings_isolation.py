"""System Settings is an isolated desktop document, never a Social feed alias."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
APP = (ROOT / "static/js/client/app.js").read_text()


def test_custom_desktop_documents_adopt_a_non_social_view_sentinel():
    focus = OS[OS.index("function focusWin(w, render)"):OS.index("let iconSpan")]
    assert "if(w.isolated)" in focus
    assert "PC().adoptView(w.view)" in focus
    assert "w.appView = w.view" in focus
    assert "adoptView: (v) => { VIEW=String(v||''); }" in APP


def test_system_settings_rerenders_cleanly_after_focus_and_ignores_stale_async_results():
    opened = OS[OS.index("function openSystemSettings()"):
                OS.index("async function renderSystemSettings()")]
    render = OS[OS.index("async function renderSystemSettings()"):
                OS.index("function openTaskManager", OS.index("async function renderSystemSettings()"))]
    assert "w.rerun=true" in opened and "w.isolated=true" in opened
    assert "host._pcOsSettings=token" in render
    assert render.count("if(!alive()) return") >= 2


def test_system_settings_exposes_the_native_power_controls_and_system_info():
    render = OS[OS.index("async function renderSystemSettings()"):
                OS.index("function openTaskManager", OS.index("async function renderSystemSettings()"))]
    for control in ("data-brightness", "data-power-profile", "data-keep-awake",
                    "data-idle-timeout", 'data-settings-page="about"'):
        assert control in render
    for method in ("pcPower.setBrightness", "pcPower.setProfile", "pcPower.setKeepAwake",
                   "pcPower.setIdleTimeout", "pcSystem.snapshot(false)"):
        assert method in render


def test_system_settings_categories_are_separate_pages_without_widget_cards():
    render = OS[OS.index("async function renderSystemSettings()"):
                OS.index("function openTaskManager", OS.index("async function renderSystemSettings()"))]
    for marker in ('data-page="displays"', 'data-page="power"', 'data-page="about"',
                   'data-settings-page="displays"', 'data-settings-page="power"',
                   'data-settings-page="about"'):
        assert marker in render
    assert "_osSettingsPage=b.dataset.page;draw()" in render
    for marker in ('data-jump="widgets"', "data-widgets", "data-widget-add",
                   "data-widget-size", "data-widget-remove", "Remove widget"):
        assert marker not in render


def test_a_missing_display_bridge_does_not_hide_unrelated_settings_pages():
    render = OS[OS.index("async function renderSystemSettings()"):
                OS.index("function openTaskManager", OS.index("async function renderSystemSettings()"))]
    assert "if(!host) return" in render
    assert "outs=window.pcDisplays?await pcDisplays.status()" in render
    assert "displayError='Could not read displays:" in render
    assert "System settings are unavailable" not in render
    public = OS[OS.index("window.PCOS = {"):]
    assert "openSystemSettings" in public


def test_mobile_settings_keeps_every_category_reachable_when_sidebar_is_hidden():
    render = OS[OS.index("async function renderSystemSettings()"):
                OS.index("function openTaskManager", OS.index("async function renderSystemSettings()"))]
    css = (ROOT / "static" / "css" / "client.css").read_text()
    assert 'data-settings-mobile' in render
    for destination in ("page:displays", "page:sound", "page:network", "page:bluetooth",
                        "page:power", "page:users", "page:updates", "page:about", "page:liveusb"):
        assert destination in render
    assert "if(kind==='page'){_osSettingsPage=value;draw();}" in render
    assert "else if(kind==='jump')jump(value,mobile)" in render
    assert ".os-set-mobile-nav{display:none}" in css
    mobile = css[css.index("@media(max-width:760px)", css.index(".os-settings-feed")):]
    assert ".os-set-nav{display:none}" in mobile
    assert ".os-set-mobile-nav{" in mobile and "display:flex" in mobile


def test_every_major_settings_category_is_a_distinct_page_not_a_combined_jump_tab():
    render = OS[OS.index("async function renderSystemSettings()"):
                OS.index("function openTaskManager", OS.index("async function renderSystemSettings()"))]
    pages = ("displays", "appearance", "sound", "network", "bluetooth", "power",
             "users", "updates", "about", "liveusb")
    for page in pages:
        assert f'data-page="{page}"' in render
        if page not in ("sound", "network", "bluetooth"):
            assert f'data-settings-page="{page}"' in render
    assert 'data-settings-page="${key}"' in render
    for combined in ('data-jump="sound"', 'data-jump="network"', 'data-jump="bluetooth"'):
        assert combined not in render
