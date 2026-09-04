from pathlib import Path


def test_notification_flyout_has_compositor_action_open_and_close_routes():
    os_js = (Path(__file__).resolve().parents[1] / "static/js/client/os.js").read_text()
    assert "p === 'pc:notifications') toggleNoti(true)" in os_js
    close = os_js[os_js.index("p === 'pc:notifications:close'") :]
    close = close[:close.index("else if(p === 'pc:shot')")]
    assert "toggleNoti(false)" in close
    assert "pcPopup.close()" in close


def test_compositor_actions_are_dispatched_only_to_shell_surfaces():
    main = (Path(__file__).resolve().parents[1] / "desktop/main.js").read_text()
    block = main[main.index("async function forwardShellTick") : main.index("const _nativeGameFullscreenAsked")]
    assert "Array.from(_shellSurfaces.values())" in block
    assert "BrowserWindow.getAllWindows()" not in block
