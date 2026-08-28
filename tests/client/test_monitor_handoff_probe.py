import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_monitor_button_rejects_right_then_moves_left_without_intermediate_recovery():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/monitor_handoff_probe_runtime.mjs")],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert "monitor handoff probe runtime: ok" in run.stdout


def test_packaged_monitor_probe_runs_against_the_supplied_os_module(tmp_path):
    packaged = tmp_path / "os.js"
    packaged.write_bytes((ROOT / "static/js/client/os.js").read_bytes())
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/monitor_handoff_probe_runtime.mjs"), str(packaged)],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr


def test_titlebar_probe_defers_frame_recovery_until_every_direction_rejects():
    source = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    block = source[source.index("async function moveToOtherMonitor"):
                   source.index("function startDrag", source.index("async function moveToOtherMonitor"))]
    assert "sendFrameHandoff(w,direction,0,false)" in block
    assert "tryMonitorDirections" in block
