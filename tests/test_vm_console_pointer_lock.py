"""The VM console can GRAB the mouse (pointer lock) so the cursor cannot wander off the VM's screen.
noVNC has no pointer lock and reads absolute clientX/clientY, so vmconsole.js keeps a virtual cursor
clamped to the canvas and re-dispatches synthetic events while locked. It is OPT-IN (click to grab,
Esc to release) and MUST be inert until locked, so a client that never grabs keeps the ordinary mouse."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "static/js/client/vmconsole.js").read_text()


def test_pointer_lock_is_wired_clamped_and_opt_in():
    assert "attachPointerLock" in JS
    assert "requestPointerLock" in JS, "must request pointer lock to grab the mouse"
    assert "pointerlockchange" in JS
    assert "movementX" in JS and "movementY" in JS, "must move a virtual cursor by raw movement deltas"
    assert "Math.max" in JS and "Math.min" in JS, "the virtual cursor must be clamped to the canvas"
    # inert until locked: the move/button handlers bail when not grabbed
    assert "if(!on || dispatching) return" in JS
    # released on console close so the capture listeners never leak
    assert "pointerLock && pointerLock.release()" in JS
    assert "exitPointerLock" in JS
