import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_encrypted_custom_reaction_wire_fixture():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/concord_custom_reaction_wire_runtime.mjs")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "encrypted Concord custom-reaction wire fixture ok" in run.stdout
