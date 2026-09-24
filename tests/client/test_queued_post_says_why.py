"""A post queued while CONNECTED must not say "when you're back online".

2026-09-24: a server fault made every reaction and share fail, and the client -- which itself showed
the user as online -- answered each one with "saved — this will send when you're back online". That
hid the fault and blamed their network, and nothing ever retried: the outbox drained on a reconnect
or an `online` event, and the socket never dropped. Runs the SHIPPED `_queuedToast` under node.
"""
import json
import re
import subprocess
from pathlib import Path

APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _fn(name):
    i = APP.index(f"function {name}(")
    depth, j = 0, APP.index("{", i)
    for k in range(j, len(APP)):
        depth += {"{": 1, "}": -1}.get(APP[k], 0)
        if depth == 0:
            return APP[i:k + 1]
    raise AssertionError(name)


def _says(msg):
    src = _fn("_queuedToast") + f"\nprocess.stdout.write(JSON.stringify(_queuedToast({json.dumps(msg)})));"
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)


def test_only_an_offline_pool_is_told_to_wait_for_the_network():
    assert "back online" in _says("offline")
    for msg in ("timeout", "blocked: not a member", "", None):
        said = _says(msg)
        assert "back online" not in said, (msg, said)
        assert "trying again" in said, (msg, said)
    assert "blocked: not a member" in _says("blocked: not a member")


def test_a_queue_filled_while_connected_is_retried_without_a_reconnect():
    block = APP[APP.index("if (queued){"):APP.index("return { ev, ...r, queued:true };")]
    assert re.search(r"if\(r\.msg\s*!==\s*'offline'\)\s*_retryOutboxSoon\(\)", block), \
        "a post queued while connected has nothing that will ever retry it"
    retry = _fn("_retryOutboxSoon")
    assert "_flushOutbox()" in retry and "setTimeout" in retry
