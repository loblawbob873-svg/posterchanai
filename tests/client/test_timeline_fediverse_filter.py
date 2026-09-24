"""Fediverse posts are part of the timeline -- there is no "hide the fediverse" any more.

That switch (on by default) existed while the Pleroma bridge mirrored whole remote timelines onto
Nostr. With the native fediverse server those posts are this node's own, so everybody -- a
logged-out visitor on the main page included -- sees them: "since we are native, no reason to hide
the fediverse from users until they opt in ... we should not even have hide fediverse from timeline
anymore". Runs the shipped _tlFilter under node, with the old preference SET, to prove nothing
reads it."""
import json
import shutil
import subprocess

import pytest

from tests.client_source import client_source

APP = client_source()
FILTER = APP[APP.index('  function _tlFilter(view)'):APP.index('  function _drawTimeline(preserveScroll)')]


def _run(js):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-2000:]


def test_fediverse_posts_are_always_in_the_timeline():
    _run(r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const prefs=new Map([['hideFediBridge',true]]);          // an old saved preference: must be ignored
const ctx={FOLLOWS:new Set(['native','followed-puppet']), ClientSettings:{get:(k,d)=>prefs.has(k)?prefs.get(k):d},
  isReply:e=>!!e.reply};
vm.createContext(ctx); vm.runInContext(''' + json.dumps(FILTER) + r''' + ';this._tlFilter=_tlFilter;', ctx);
const stranger={pubkey:'other-puppet',tags:[['proxy','https://fedi.test/1','activitypub']]};
const followed={pubkey:'followed-puppet',tags:[['fedibridge','x']]};
assert.equal(ctx._tlFilter('global')(stranger),true,'an unfollowed fediverse account was hidden from the global timeline');
assert.equal(ctx._tlFilter('home')(followed),true,'a followed fediverse account was hidden from Home');
assert.equal(ctx._tlFilter('home')({pubkey:'nobody',tags:[]}),false,'home shows only follows');
prefs.set('hideReplies',true);
assert.equal(ctx._tlFilter('global')({pubkey:'native',tags:[],reply:true}),false);
''')


def test_the_switch_is_gone_everywhere():
    for needle in ("hideFediBridge", "set-hide-fedi", "isFediBridged"):
        assert needle not in APP, f"{needle} is still in the client"
