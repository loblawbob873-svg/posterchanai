"""A private repo made (or edited) in the web UI is announced to THIS node's relay -- actually sent, and only there.

Vyram, 2026-10-18: "tried to make another private repo, says created, private announcement not yet confirmed, this
node's relay hasn't answered" -- every time. The relay was never asked: `Relay.publishTo` skips relays already in the
connection pool (it exists for EXTERNAL relays), this node's relay is always pooled, so the announcement went
nowhere and the call answered 0. The old test read the source for `Relay.publishTo([home]` and passed throughout.
Editing a private repo had the opposite bug: `publish()` writes to every pooled relay, handing a private repo's
name and maintainers to all of them. Both now go through `Relay.publishHome`, run here as shipped.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/publish_home_runtime.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:3000]
    return json.loads(out.stdout)


def test_the_pooled_home_relay_is_actually_sent_the_announcement():
    got = _run({"pooled": True, "call": "home"})
    assert got["delivered"] == ["wss://relay.example"], got
    assert got["r"]["ok"] is True, got


def test_the_old_call_sent_nothing_which_is_what_vyram_saw():
    got = _run({"pooled": True, "call": "oldPublishTo"})
    assert got["delivered"] == [] and got["r"] == 0, got


def test_a_refusal_says_so_and_silence_is_uncertain_not_refused():
    ref = _run({"pooled": True, "call": "home", "refuse": True})["r"]
    assert ref["ok"] is False and not ref["uncertain"] and "blocked" in ref["msg"], ref
    sil = _run({"pooled": True, "call": "home", "silent": True})["r"]
    assert sil["ok"] is False and sil["uncertain"] is True, sil


def test_no_home_relay_is_an_answer_not_a_send_elsewhere():
    got = _run({"noHome": True, "call": "home"})
    assert got["delivered"] == [] and got["r"]["ok"] is False, got


def test_every_private_repo_path_in_git_js_goes_through_publish_home():
    src = (ROOT / "static/js/client/git.js").read_text()
    assert not re.search(r"Relay\.publishTo\(\s*\[\s*home\s*\]", src), "the call that sends nothing is back"
    start = src.index("async function _doCreateRepo")
    create = src[start:src.index("\n  }\n", start)]
    assert "Relay.publishHome(" in create.split("if(!priv &&")[0]
    edit = src[src.index("const pubRepo=async"):src.index("const r=await pubRepo()")]
    assert "_repoIsPrivate(" in src[src.index("const priv=_repoIsPrivate"):][:40]
    assert "Relay.publishHome(" in edit and "if(!priv) return publish(30617" in edit


def test_the_default_pool_names_this_node_as_home():
    """No personal relay list (most accounts): the pool is Relay.connect(instance relay), which never set _home --
    so a private repo said "this node's relay is not connected" and every home-answered write gate was blind."""
    got = _run({"pooled": "default", "call": "home"})
    assert got["delivered"] == ["wss://relay.example"] and got["r"]["ok"] is True, got
