from pathlib import Path


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


def test_control_is_revocable_and_dies_with_the_call():
    assert "_rdSend({t:'release'})" in APP
    assert "m.t==='release'&&_call.caller" in APP
    assert "if(_call.control) _call.control.close()" in APP
    assert "call-control-stop" in APP
    assert "_rdReleaseNative()" in APP


def test_remote_packets_are_bounded_before_crossing_the_native_bridge():
    assert "String(e.data||'').length>512" in APP
    assert "Math.max(-240,Math.min(240" in APP
    assert "window.pcRemoteControl&&pcRemoteControl.input" in APP
    assert "const _RD_KEYS=" in APP
