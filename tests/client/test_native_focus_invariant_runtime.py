from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def test_overlay_return_redecorates_before_native_focus_at_runtime():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/native_focus_invariant_runtime.js")],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert "Native focus invariant runtime: ok" in run.stdout


def test_no_owned_native_return_path_uses_raw_focus():
    menu = OS[OS.index("async function _nativeMenuLayer"):
              OS.index("let _natFocusHold", OS.index("async function _nativeMenuLayer"))]
    gesture = OS[OS.index("function _natGesture"):
                 OS.index("const _zOf", OS.index("function _natGesture"))]
    assert "_focusNativeDecorated(focused.id,token)" in menu
    assert "_focusNativeDecorated(w.native,focusToken)" in gesture
    assert "pcWM.focus(focused.id)" not in menu
    assert "pcWM.focus(w.native)" not in gesture
    taskbar = OS[OS.index("if(b.dataset.kind === 'native')"):
                 OS.index("const w = wins.find", OS.index("if(b.dataset.kind === 'native')"))]
    assert "_focusNativeDecorated(w.id,focusToken)" in taskbar
    assert "pcWM.focus(w.id)" not in taskbar
