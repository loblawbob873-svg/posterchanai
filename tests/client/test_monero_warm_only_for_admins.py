"""The node's wallet is warmed only for somebody who can open it (see the .mjs for the report)."""
import subprocess
from pathlib import Path


def test_a_member_is_never_probed_for_the_admin_only_node_wallet():
    r = subprocess.run(["node", str(Path(__file__).with_name("monero_warm_only_for_admins_runtime.mjs"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
