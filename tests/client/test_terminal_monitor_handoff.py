from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_terminal_monitor_handoff_runtime_uses_session_identity_and_returns_cleanly():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/terminal_handoff_runtime.mjs")],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    assert "terminal monitor handoff runtime: ok" in run.stdout


def test_os_transfers_terminal_state_before_opening_destination_window():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    payload = src[src.index("function handoffPayload"):src.index("function sendFrameHandoff")]
    destination = src[src.index("if(pcWM.onHandoffFrame)"):src.index("if(pcWM.onPreviewFrame)")]
    assert "PCTerm.handoffState()" in payload
    assert "PCTerm.acceptHandoff(p.state)" in destination
    assert destination.index("PCTerm.acceptHandoff(p.state)") < destination.index("const w=openApp")
