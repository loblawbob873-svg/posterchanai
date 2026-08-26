from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8", errors="ignore")
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def test_desktop_uses_electrons_native_notification_bridge():
    assert "ipcMain.handle('pc:host:notify'" in MAIN
    assert "Notification.isSupported()" in MAIN
    assert "notify: (opts) => ipcRenderer.invoke('pc:host:notify'" in PRELOAD
    assert "if(window.pcHost&&pcHost.notify)" in APP


def test_native_notification_click_focuses_the_app_and_preserves_route():
    assert "owner.show();owner.focus()" in MAIN
    assert "pc:host:notification-click" in MAIN
    assert "onNotificationClick" in PRELOAD
    assert "pcHost.onNotificationClick(route=>" in APP


def test_dms_and_community_mentions_use_the_shared_os_notifier():
    assert "osNotify('✉ New message'" in APP
    assert "p.osNotify(`Mention in #" in CONCORD
