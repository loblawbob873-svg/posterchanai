"""A server that is restarting is never offered for removal as "not a PosterChan server".

2026-10-18, the owner's tablet: "classic mode is saying 'this app is set to run without a server (relays only)' -- I
never did this". Every boot probes the instance's /client/config, and ANY non-2xx answered 'not' -- so a boot during
a deploy (the proxy's 502, the app's 503 while it loads) popped "poster.place is not a PosterChan server... run
without a server instead?", and one tap removed the instance: widgets dead, Music saying "add music", no wallet.
Now a 5xx/429/no answer is 'down', and the boot warning needs 'not' twice, a minute apart.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/instance_probe_runtime.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
PC = json.dumps({"relay_url": "wss://relay.poster.place", "nostr_only": False})


def _run(plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:3000]
    return json.loads(out.stdout)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 521, 429])
def test_a_server_mid_restart_is_down_not_foreign(status):
    assert _run({"answers": [{"status": status, "body": "<html>bad gateway</html>"}]})["out"] == "down"


def test_no_answer_is_down():
    assert _run({"answers": ["throw"]})["out"] == "down"


def test_a_real_server_is_ok_and_a_relay_is_not():
    assert _run({"answers": [{"status": 200, "body": PC}]})["out"] == "ok"
    assert _run({"answers": [{"status": 200, "body": '{"name":"a relay","supported_nips":[1]}'}]})["out"] == "not"
    assert _run({"answers": [{"status": 404, "body": "nope"}]})["out"] == "not"


def test_the_boot_warning_needs_two_foreign_answers():
    one = _run({"confirm": True, "answers": [{"status": 404, "body": "x"}, {"status": 200, "body": PC}]})
    assert one["out"] is False and one["asked"] == 2, one
    two = _run({"confirm": True, "answers": [{"status": 404, "body": "x"}, {"status": 404, "body": "x"}]})
    assert two["out"] is True, two
    down = _run({"confirm": True, "answers": [{"status": 502, "body": "x"}]})
    assert down["out"] is False and down["asked"] == 1, down
