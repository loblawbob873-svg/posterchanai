"""A community left on one device stays left everywhere (see the .mjs for the report)."""
import subprocess
from pathlib import Path


def test_a_leave_reaches_the_account_even_from_a_device_that_never_opened_the_room():
    script = Path(__file__).with_name("concord_leave_reaches_the_account_runtime.mjs")
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
