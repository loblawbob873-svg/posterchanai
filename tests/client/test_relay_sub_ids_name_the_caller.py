"""A client subscription id names the file that opened it (relay.js `_callerTag`) -- see the .mjs."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_a_subscription_id_names_the_calling_file():
    r = subprocess.run(["node", str(ROOT / "tests/client/relay_sub_ids_name_the_caller_runtime.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
