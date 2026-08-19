"""Replying to a NIP-34 issue/patch must publish a NIP-22 comment, not a kind-1 note.

Current NIP-34 dropped kind-1622 replies — issue discussion is kind-1111 comments with the root
repeated in uppercase E/K/P tags — and gitworkshop renders ONLY those. This client's `replyKindFor`
used to emit 1111 solely when the parent was already an 1111, so every reply typed into an issue
thread here went out as a plain kind-1: threaded fine in this client, invisible on gitworkshop
(measured: every reply on this repo's 38 issues). The kind-1 fallback for ordinary notes and the
rootless-1111 guard must BOTH survive the change, so all three shapes are driven here.

Runs the REAL _commentScope/replyKindFor/replyTags out of app.js under node (the test_call_ring_filter
pattern) — a reimplementation would agree with itself while production disagreed.

Run: venv-unified/bin/python -m pytest tests/test_client_reply_kind.py
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "client" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _reply_for(parent):
    """Run the shipped reply-kind logic over `parent`, returning {kind, tags}."""
    src = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function _commentScope\(parent\)\{.*?\n  function replyTags\(parent, id, pk\)\{"
                  r".*?\n  \}", src, re.S)
    assert m, "could not find _commentScope/replyKindFor/replyTags in app.js — did they move?"
    js = ("const CFG={relay_url:'wss://r'};\n" + m.group(0) + """
        const parent = JSON.parse(process.argv[1]);
        console.log(JSON.stringify({ kind: replyKindFor(parent),
                                     tags: replyTags(parent, parent.id, parent.pubkey) }));""")
    out = subprocess.run(["node", "-e", js, json.dumps(parent)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


ISSUE = {"id": "d" * 64, "pubkey": "b" * 64, "kind": 1621, "created_at": 1, "content": "bug",
         "tags": [["a", "30617:" + "a" * 64 + ":posterchanai"], ["subject", "bug"],
                  ["p", "a" * 64]]}


def _tag(tags, name):
    return [t for t in tags if t and t[0] == name]


def test_an_issue_reply_is_a_nip22_comment():
    r = _reply_for(ISSUE)
    assert r["kind"] == 1111, "a reply to a 1621 issue must be kind 1111 — kind 1 is invisible on gitworkshop"
    tags = r["tags"]
    assert _tag(tags, "E")[0][1] == ISSUE["id"]
    assert _tag(tags, "K")[0][1] == "1621"
    assert _tag(tags, "P")[0][1] == ISSUE["pubkey"]
    # lowercase parent = the root itself for a top-level comment, and its kind travels in `k`
    assert _tag(tags, "e")[0][1] == ISSUE["id"]
    assert _tag(tags, "k")[0][1] == "1621"
    # the repo owner's p-tag on the issue is carried forward, so maintainers stay notified
    assert any(t[1] == "a" * 64 for t in _tag(tags, "p"))


def test_a_patch_reply_too():
    r = _reply_for(dict(ISSUE, kind=1617))
    assert r["kind"] == 1111
    assert _reply_for(dict(ISSUE, kind=1618))["kind"] == 1111


def test_an_ordinary_note_still_gets_a_kind1_nip10_reply():
    note = {"id": "e" * 64, "pubkey": "b" * 64, "kind": 1, "created_at": 1, "content": "gm",
            "tags": []}
    r = _reply_for(note)
    assert r["kind"] == 1
    assert _tag(r["tags"], "e")[0][3] == "root"
    assert not _tag(r["tags"], "E")


def test_a_rootless_1111_still_falls_back_to_kind1():
    # The guard that predates this change: a 1111 with no uppercase scope must not spawn a rootless
    # 1111 (clients that thread NIP-22 strictly would orphan it).
    stray = {"id": "f" * 64, "pubkey": "b" * 64, "kind": 1111, "created_at": 1, "content": "x",
             "tags": [["e", "9" * 64]]}
    assert _reply_for(stray)["kind"] == 1


def test_a_scoped_1111_reply_stays_in_its_thread():
    comment = {"id": "f" * 64, "pubkey": "b" * 64, "kind": 1111, "created_at": 1, "content": "x",
               "tags": [["E", "d" * 64], ["K", "1621"], ["P", "c" * 64], ["e", "d" * 64]]}
    r = _reply_for(comment)
    assert r["kind"] == 1111
    assert _tag(r["tags"], "E")[0][1] == "d" * 64   # root scope copied verbatim, not rebuilt
    assert _tag(r["tags"], "e")[0][1] == comment["id"]   # lowercase parent = the comment replied to
