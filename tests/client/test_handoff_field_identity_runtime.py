import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_handoff_forms_restore_by_identity_when_destination_dom_order_changes():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/handoff_field_identity_runtime.mjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "handoff field identity runtime: ok" in run.stdout
