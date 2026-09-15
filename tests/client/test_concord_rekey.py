"""Independent CORD06 transport vectors exercise the shipped reader, not a mock fold."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_concord_rekey_runtime():
    result = subprocess.run(["node", "tests/client/concord_rekey_runtime.mjs"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "authenticated rekey lifecycle passed" in result.stdout
