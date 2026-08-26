from pathlib import Path
import json
import shutil
import subprocess
import textwrap

import pytest

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


def test_same_login_has_a_direct_action_and_failures_are_visible_inline():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert "data-rd-self>Share to my other signed-in device" in OS
    assert "selfButton.onclick=()=>" in OS
    assert "go(pk)" in OS
    assert "data-rd-status role=\"status\" aria-live=\"polite\"" in OS
    assert "status.textContent=String((e&&e.message)||e)" in OS
    assert "Screen sharing was cancelled or could not start." in OS
    assert "[data-rd-choose][hidden]{display:none!important}" in css


def test_desktop_uses_its_source_picker_on_linux_instead_of_the_failing_portal_path():
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    assert "useSystemPicker: process.platform === 'darwin'" in main
    assert "useSystemPicker: true" not in main
    assert "process.platform === 'linux' ? ['screen'] : ['screen', 'window']" in main
    assert ".catch((error) => {" in main
    assert "pickerOpen = false;" in main


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_address_discovery_returns_choices_then_resolves_the_selected_user(tmp_path):
    """Execute the shipped resolver. An IP with two users must populate the picker, and choosing
    one must issue a name-scoped lookup and return that user's key and signaling relay."""
    start = APP.index("  const _remoteDesktopResolved=new Map();")
    end = APP.index("\n  async function startRemoteDesktop(peer){", start)
    resolver = APP[start:end]
    driver = tmp_path / "remote-address.js"
    driver.write_text(textwrap.dedent(f"""
      const calls=[];
      const BUNDLED=true;
      global.window={{isSecureContext:true}};
      global.safePk=s=>/^[0-9a-f]{{64}}$/.test(s)?s:'';
      global.normalizeRelay=s=>String(s||'');
      global.fetch=async url=>{{
        calls.push(url);
        const selected=url.includes('?name=alice');
        return {{ok:true,json:async()=>selected
          ? {{names:{{alice:'a'.repeat(64)}},relays:{{['a'.repeat(64)]:['wss://peer.test']}}}}
          : {{names:{{alice:'a'.repeat(64),bob:'b'.repeat(64)}},relays:{{['a'.repeat(64)]:['wss://peer.test']}}}}}};
      }};
      {resolver}
      (async()=>{{
        let choices=[];
        try{{await _remoteDesktopAddress('10.0.0.8')}}catch(e){{choices=e.remoteChoices||[];}}
        const target=await _remoteDesktopAddress(choices[0].value);
        console.log(JSON.stringify({{choices,target,calls}}));
      }})().catch(e=>{{console.error(e);process.exit(1)}});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert [x["value"] for x in result["choices"]] == ["a" * 64, "b" * 64]
    assert result["target"] == {"pk": "a" * 64, "relays": ["wss://peer.test"]}
    assert len(result["calls"]) == 1, "selection must not spend the screen-picker click on another fetch"


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
    assert PHONE.count("landView(v)") == 2  # declaration + the one serialized call site
    assert PHONE.count("consumeLaunchView(") >= 4


def test_wifi_panel_is_repositioned_after_async_results_change_its_size():
    i = SHELL.index("body.innerHTML = (list.length")
    assert "positionPop(d,_popAnchor,_popOpts)" in SHELL[i:i + 1200]
