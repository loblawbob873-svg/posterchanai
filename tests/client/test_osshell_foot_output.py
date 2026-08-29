from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_foot_title_bursts_do_not_run_the_desktop_reconciler_twice():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/osshell_foot_output_runtime.js")],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert "Foot sustained-output watcher runtime: ok" in run.stdout
