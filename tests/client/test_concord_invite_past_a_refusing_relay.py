"""Creating an invite link to your own community works when one of its relays REFUSES the read (relay.damus.io).

2026-10-26: "i get a error when creating an invite link to my own room, complains about damus". The invite registry is
re-read from every relay of the community before a link is added, and it demanded that every one ANSWER. relay.damus.io
answers that kind-1059 read with auth-required, and again after the member's AUTH (only the stream key may read there),
so the read was always "incomplete -- 1 relay(s) did not answer (wss://relay.damus.io)" and no link could ever be made.
relay.js now records such a relay as `refused` (still `failed` for everyone else), and Concord's two invite reads excuse
a refusal -- only when some other relay did answer. tests/client/relay_refused_read_runtime.mjs runs the shipped code.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_relay_that_refuses_the_read_is_not_a_missing_answer():
    r = subprocess.run(["node", "tests/client/relay_refused_read_runtime.mjs", "static/js/client/relay.js",
                        "static/js/client/concord.js"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "PASS" in r.stdout, (r.stdout[-2000:], r.stderr[-2000:])


def test_both_invite_reads_use_it():
    src = (ROOT / "static/js/client/concord.js").read_text()
    reg = src[src.index("async function ownedInviteRegistry("):src.index("const ownedInviteWrites=")]
    assert "unansweredBesidesRefusals(scope.relays,report)" in reg
    ctx = src[src.index("async function ownedInviteContext("):src.index("async function ownedInviteContext(") + 4000]
    assert "unansweredBesidesRefusals(targets,report)" in ctx
