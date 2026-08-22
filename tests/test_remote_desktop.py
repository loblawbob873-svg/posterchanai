from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
PHONE = (ROOT / "static/js/client/phoneshell.js").read_text(encoding="utf-8")
SHELL = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")


def test_remote_desktop_is_a_real_launcher_app():
    assert "label: 'Remote Desktop'" in OS
    assert "PC().startRemoteDesktop(peer)" in OS


def test_remote_desktop_sends_a_screen_and_no_guest_media():
    assert "navigator.mediaDevices.getDisplayMedia" in APP
    assert "if(remoteGuest) return Promise.resolve(new MediaStream())" in APP
    assert "remoteDesktop,\n                              sdp:" in APP
    assert "remoteDesktop:!!msg.remoteDesktop" in APP


def test_launcher_tiles_leave_desktop_without_forgetting_the_preference():
    assert "mobileLanding: () => { if(on) exit(false); }" in OS
    assert PHONE.count("PCOS.mobileLanding()") >= 2


def test_wifi_panel_is_repositioned_after_async_results_change_its_size():
    i = SHELL.index("body.innerHTML = (list.length")
    assert "positionPop(d,_popAnchor,_popOpts)" in SHELL[i:i + 1200]
