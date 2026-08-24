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
                    "data-idle-timeout", "data-about"):
        assert control in render
    for method in ("pcPower.setBrightness", "pcPower.setProfile", "pcPower.setKeepAwake",
                   "pcPower.setIdleTimeout", "pcSystem.snapshot(false)"):
        assert method in render
