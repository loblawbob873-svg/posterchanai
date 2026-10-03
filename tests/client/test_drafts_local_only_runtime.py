"""A Nostr-only account's drafts: the client is told the server keeps nothing for it, does not retry a
refusal that will never change, and says once that drafts live on this device (backlog #71). Runs the
shipped drafts.js factory under node with a fake fetch."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
global.window = global; global.localStorage = { _m: {}, getItem(k){ return this._m[k] || null; }, setItem(k, v){ this._m[k] = v; } };
require(%(src)s);
const toasts = [], calls = [];
global.fetch = async (url, o) => { calls.push(JSON.parse(o.body)); return { ok: true, json: async () => (%(answer)s) }; };
const noop = () => {};
const dep = { state: { ME: { pubkey: 'a'.repeat(64) }, VIEW: 'home', CFG: {} },
  $: () => null, $$: () => [], toast: m => toasts.push(m), selfProof: async () => 'proof', bumpMoreBadge: noop,
  NT: noop, _draftSnapshot: noop, _enrichTags: noop, _qDraftSet: noop, _qDrafts: () => ({}), _queuedDraftMatches: noop,
  _reconcileDeliveredDrafts: noop, compose: noop, enc: x => x, fetchEvent: noop, hydrate: noop, linkify: x => x,
  mentionTags: noop, publish: noop, replyKindFor: noop, replyTags: noop, sign: noop, timeAgo: noop, uiConfirm: noop };
const mod = window.PCDraftsFactory(dep);
const Drafts = mod.Drafts || mod;
Drafts.save({ text: 'hello' });
Drafts.save({ text: 'again' });
setTimeout(() => console.log(JSON.stringify({ posts: calls.length, toasts })), 6500);
"""


def _run(answer):
    script = SCRIPT % {"src": json.dumps(str(ROOT / "static/js/client/drafts.js")), "answer": json.dumps(answer)}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_local_only_answer_is_not_retried_and_is_said_once():
    r = _run({"ok": False, "local_only": True, "error": "drafts are kept on this device only"})
    assert r["posts"] == 1, ("a refusal that cannot change was retried", r)
    assert len(r["toasts"]) == 1 and "this device only" in r["toasts"][0], r
    assert "could not be saved" not in r["toasts"][0], ("it reads as a server failure", r)


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_real_refusal_is_still_retried_and_reported():
    r = _run({"ok": False, "error": "drafts unavailable, not saved"})
    assert r["posts"] == 3 and any("could not be saved" in t for t in r["toasts"]), r
