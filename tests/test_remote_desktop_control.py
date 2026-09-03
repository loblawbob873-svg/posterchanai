import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text()


def test_control_uses_a_private_ordered_webrtc_channel():
    assert "pc.createDataChannel('posterchan-control',{ordered:true})" in APP
    assert "pc.ondatachannel=e=>" in APP
    assert "e.channel.label==='posterchan-control'" in APP


def test_viewer_must_request_and_host_must_explicitly_grant_control():
    assert "_rdSend({t:'request'})" in APP
    assert "_call.controlRequested=true" in APP
    assert "call-control-allow" in APP
    assert "call-control-deny" in APP
    assert "_rdGrant(true)" in APP and "_rdGrant(false)" in APP
    gate = "m.t==='input'&&_call.caller&&_call.controlGranted"
    assert gate in APP


def test_remote_desktop_is_armed_only_while_its_app_is_open_on_both_ends():
    os_src = (ROOT / "static/js/client/os.js").read_text()
    assert "let _remoteDesktopArmed=false" in APP
    assert "if(!_remoteDesktopArmed)throw new Error('open Remote Desktop before starting a session')" in APP
    assert "if(remoteDesktopInvite&&!_remoteDesktopArmed)return" in APP
    assert "setRemoteDesktopArmed&&PC().setRemoteDesktopArmed(true)" in os_src
    assert "w.onClose=()=>" in os_src
    assert "setRemoteDesktopArmed&&PC().setRemoteDesktopArmed(false)" in os_src


def test_same_identity_remote_desktop_auto_accepts_on_the_other_device_only():
    assert "const _CALL_DEVICE_ID=" in APP
    assert "Object.assign({},obj,{deviceId:_CALL_DEVICE_ID})" in APP
    assert "msg.deviceId===_CALL_DEVICE_ID)return" in APP
    assert "if(remoteDesktopInvite&&from===ME.pubkey){_acceptCall().catch(()=>{});return;}" in APP
    # Consent is still required before the self-device auto-answer branch can be reached.
    assert APP.index("if(remoteDesktopInvite&&!_remoteDesktopArmed)return") < APP.index(
        "if(remoteDesktopInvite&&from===ME.pubkey)")


def test_control_is_revocable_and_dies_with_the_call():
    assert "_rdSend({t:'release'})" in APP
    assert "m.t==='release'&&_call.caller" in APP
    assert "if(_call.control) _call.control.close()" in APP
    assert "call-control-stop" in APP
    assert "_rdReleaseNative()" in APP


def test_remote_packets_are_bounded_before_crossing_the_native_bridge():
    assert "String(e.data||'').length>512" in APP
    assert "Math.max(-12,Math.min(12,Math.sign(e.deltaY)))" in APP
    assert "window.pcRemoteControl&&pcRemoteControl.input" in APP
    assert "const _RD_KEYS=" in APP


def test_native_host_freezes_and_configures_the_captured_display():
    main = (ROOT / "desktop/main.js").read_text()
    preload = (ROOT / "desktop/preload.js").read_text()
    assert "remoteControlDisplayId=d ? String(d.id) : ''" in main
    assert "if(source.display_id){ remoteControlDisplayId=String(source.display_id)" in main
    assert "ipcMain.handle('pc:remote:configure'" in main
    assert "ranked[0].score<ranked[1].score" in main
    assert "configure: info => ipcRenderer.invoke('pc:remote:configure'" in preload
    assert "_rdConfigureNative(local)" in APP
    assert "_rdConfigureNative(next)" in APP


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_pointer_mapping_tracks_the_contained_video_after_viewer_resize():
    start = APP.index("  function _rdVideoPoint(video,e){")
    end = APP.index("  function _rdBindViewer(video){", start)
    fn = APP[start:end]
    js = f"""
      {fn}
      const video={{videoWidth:1920,videoHeight:1080,
        getBoundingClientRect:()=>({{left:0,top:0,width:800,height:600}})}};
      const points=[_rdVideoPoint(video,{{clientX:0,clientY:75}}),
                    _rdVideoPoint(video,{{clientX:800,clientY:525}}),
                    _rdVideoPoint(video,{{clientX:400,clientY:0}})];
      video.getBoundingClientRect=()=>({{left:0,top:0,width:1200,height:600}});
      points.push(_rdVideoPoint(video,{{clientX:600,clientY:300}}));
      console.log(JSON.stringify(points));
    """
    run = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    points = json.loads(run.stdout)
    assert points[0] == {"x": 0, "y": 0}
    assert points[1] == {"x": 1, "y": 1}
    assert points[2]["y"] == 0, "top letterbox must clamp to the remote screen edge"
    assert points[3]["x"] == pytest.approx(.5)
    assert points[3]["y"] == pytest.approx(.5)
