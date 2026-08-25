from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
PHONE = (ROOT / "static/js/client/phoneshell.js").read_text(encoding="utf-8")
SHELL = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")


def test_remote_desktop_is_a_real_launcher_app():
    assert "label: 'Remote Desktop'" in OS
    assert "PC().startRemoteDesktop(peer)" in OS
    assert "Viewer’s npub or address" in OS
    assert "name@host" in OS


def test_remote_desktop_resolves_ip_without_trusting_it_as_identity():
    assert "async function _remoteDesktopAddress(peer)" in APP
    assert "'/.well-known/nostr.json'" in APP
    assert "e.remoteChoices=names.map" in APP
    assert "signalRelays:target.relays" in APP
    assert "Relay.subscribeFrom(signalRelays" in APP
    relay=(ROOT / "static/js/client/relay.js").read_text(encoding="utf-8")
    assert "worker.call('verifyBatch',{events:[m[2]]})" in relay
    assert "if(_call.signalClose) _call.signalClose()" in APP
    assert "'http://'+host+':3051'" in APP
    assert "const pk=names[0].pk" in APP


def test_multi_user_address_has_an_explicit_continue_action():
    """The first option is selected before the picker appears, so onchange cannot be the only way
    forward. Screen capture also needs a direct user gesture; keep an actual continuation button."""
    assert "data-rd-continue>Share with this user" in OS
    assert "continueButton.onclick=()=>" in OS
    assert "input.value=choice.value" in OS
    assert "choose.hidden=true;go()" in OS


def test_remote_desktop_sends_a_screen_and_no_guest_media():
    assert "navigator.mediaDevices.getDisplayMedia" in APP
    assert "if(remoteGuest) return Promise.resolve(new MediaStream())" in APP
    assert "remoteDesktop,\n                              sdp:" in APP
    assert "remoteDesktop:!!msg.remoteDesktop" in APP


def test_stopping_browser_screen_share_ends_the_remote_desktop_session():
    assert "screen.addEventListener('ended'" in APP
    assert "_call.local===local && _call.remoteDesktop" in APP
    assert "_hangup(false)" in APP


def test_remote_desktop_viewer_has_a_real_fullscreen_control():
    assert "act('call-full','⛶','Fullscreen')" in APP
    assert "await el.requestFullscreen()" in APP
    assert "await document.exitFullscreen()" in APP


def test_launcher_tiles_leave_desktop_without_forgetting_the_preference():
    assert "mobileLanding: () => { if(on) exit(false); }" in OS
    # Both cold/resume paths go through the one boot-ordered landing function.
    assert PHONE.count("PCOS.mobileLanding()") == 1
    assert PHONE.count("landView(v)") >= 3


def test_wifi_panel_is_repositioned_after_async_results_change_its_size():
    i = SHELL.index("body.innerHTML = (list.length")
    assert "positionPop(d,_popAnchor,_popOpts)" in SHELL[i:i + 1200]
