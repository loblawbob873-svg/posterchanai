"""The bell is not stuck at 1 by an update row: 'on desktop I have a notification bell stuck at 1
despite clicking on it many times'. The desktop's bundled client was older than the server's, and the
bell counted the 'App update available' row for as long as the row existed."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_opening_notifications_clears_the_update_from_the_bell():
    run = subprocess.run(["node", str(ROOT / "tests/client/update_row_bell_runtime.mjs")], cwd=ROOT,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "update row bell runtime ok" in run.stdout
