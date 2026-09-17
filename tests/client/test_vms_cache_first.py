"""Virtual Machines paints from its cache before the network, waits for a socket before querying, and
never renders silence as an empty host. Runs the SHIPPED static/js/client/vms.js under node — see
tests/client/vms_cache_first_runtime.mjs for the scenarios and why each one exists.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_vms_cache_first_and_no_answer_rules():
    r = subprocess.run(["node", str(ROOT / "tests/client/vms_cache_first_runtime.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL OK" in r.stdout


def test_the_vmhosts_doc_is_pinned_and_carried():
    """Evicted from the cache or left behind on a relay change, the host list reads as EMPTY — which
    looks like the hosts are gone. Both registries, by name (every private doc has missed one)."""
    store = (ROOT / "static/js/client/store.js").read_text()
    app = (ROOT / "static/js/client/app.js").read_text()
    pin = store[store.index("function _isPinned(ev)"):]
    pin = pin[:pin.index("\n  }\n")]
    assert "'pcai:vmhosts'" in pin
    carry = app[app.index("const _CARRY_D = ["):]
    carry = carry[:carry.index("];")]
    assert "/^pcai:vmhosts$/" in carry
