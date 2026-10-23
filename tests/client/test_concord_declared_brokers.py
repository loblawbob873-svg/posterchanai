"""A community's declared call servers (`av_brokers`) decide where its calls go -- see the runtime's header."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_declared_call_servers_choose_the_broker():
    r = subprocess.run(["node", str(ROOT / "tests/client/concord_declared_brokers_runtime.mjs")], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK declared call servers" in r.stdout, r.stdout[-2000:] + r.stderr[-4000:]
