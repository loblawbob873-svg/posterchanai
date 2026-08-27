from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
OS = (ROOT / "static/js/client/os.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_native_monitor_handoff_prepares_and_acks_before_moving_surface():
    handler = MAIN.split("ipcMain.handle('pc:wm:handoff'", 1)[1].split(
        "ipcMain.handle('pc:wm:handoff-frame'", 1)[0]
    assert "pc:wm:native-handoff-prepare" in handler
    assert "runAtomicHandoff" in handler
    assert handler.index("pc:wm:native-handoff-prepare") < handler.index("move container to workspace")
    assert "nativeHandoffAck" in PRELOAD
    assert "pc:wm:native-handoff-abort" in handler
    assert "BrowserWindow.getAllWindows" not in handler


def test_destination_adopts_and_decorates_before_periodic_reconciliation():
    assert "onNativeHandoff" in PRELOAD
    receiver = OS.split("if(pcWM.onNativeHandoff)", 1)[1].split(
        "if(pcWM.onHandoffFrame)", 1)[0]
    assert "adoptNative(row)" in receiver
    assert "pcWM.decorate(id)" in receiver
    assert "requestAnimationFrame(()=>nsync())" in receiver
    assert "_nativeHandoffOff()" in OS


def test_prepared_frame_is_hidden_and_excluded_from_native_reconciliation():
    assert "native-handoff-prepared{visibility:hidden;pointer-events:none}" in CSS
    assert "if(it.w.nativeHandoffToken) continue" in OS
    prepare = OS.split("if(pcWM.onNativeHandoffPrepare)", 1)[1].split(
        "if(pcWM.onNativeHandoff)", 1)[0]
    assert "NAT().mapRect(_bodyRect(w),scale)" in prepare
    assert "pcWM.nativeHandoffAck(token,rect)" in prepare


def test_dragging_an_html_terminal_temporarily_parks_only_overlapping_native_pixels():
    gesture = OS.split("function _natGesture", 1)[1].split("const _zOf", 1)[0]
    overlays = OS.split("function overlayRects", 1)[1].split("let _natObs", 1)[0]
    assert "w.native == null" in gesture
    assert "_htmlGestureRect = on ? _frameRect(w) : null" in gesture
    assert "if(nativeWins().length) nsync()" in gesture
    assert "if(_htmlGestureRect) out.push" in overlays


def _transaction(case):
    module = ROOT / "desktop" / "native-handoff.js"
    js = r"""
const {runAtomicHandoff}=require(process.argv[1]);
const which=process.argv[2], events=[];
const ops={
 prepare:()=>{events.push('prepare'); return which==='timeout'?new Promise(()=>{}):{x:1};},
 commit:()=>{events.push('commit'); if(which==='failure')throw Error('move failed'); return 'ok';},
 rollback:()=>{events.push('rollback');}, abort:()=>{events.push('abort');}
};
runAtomicHandoff(ops,20).then(result=>console.log(JSON.stringify({result,events})));
"""
    out = subprocess.check_output(["node", "-e", js, str(module), case], text=True)
    return json.loads(out)


def test_atomic_transaction_commits_only_after_prepare_ack():
    assert _transaction("success") == {"result": "ok", "events": ["prepare", "commit"]}


def test_atomic_transaction_timeout_aborts_without_moving():
    assert _transaction("timeout") == {"result": False, "events": ["prepare", "abort"]}


def test_atomic_transaction_move_failure_rolls_back_then_removes_prepared_frame():
    assert _transaction("failure") == {
        "result": False, "events": ["prepare", "commit", "rollback", "abort"]}


def test_native_recovery_surface_is_never_plain_black():
    rule = CSS.split(".osw-native .osw-body{", 1)[1].split("}", 1)[0]
    assert "var(--panel" in rule
    assert "#05050c" not in rule
