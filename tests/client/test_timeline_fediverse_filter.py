"""The timeline's fediverse filter ("Hide fediverse posts in timelines", on by default).

It hides fediverse posts from people you do NOT follow -- Home and Nostrverse otherwise fill with
everything anybody here follows. It must never hide someone you DO follow: following a fediverse
account is how its posts reach you now, and hiding them made every fediverse follow invisible on
Home. Runs the shipped isFediBridged/_tlFilter under node."""
import json
import shutil
import subprocess

import pytest

from tests.client_source import client_source

APP = client_source()
FILTER = APP[APP.index('  function isFediBridged(ev)'):APP.index('  function _drawTimeline(preserveScroll)')]


def _run(js):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-2000:]


def test_followed_fediverse_accounts_are_never_hidden():
    _run(r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const prefs=new Map();
const ctx={FOLLOWS:new Set(['native','followed-puppet']), ClientSettings:{get:(k,d)=>prefs.has(k)?prefs.get(k):d},
  isReply:e=>!!e.reply};
vm.createContext(ctx); vm.runInContext(''' + json.dumps(FILTER) + r''' + ';this._tlFilter=_tlFilter;', ctx);
const native={pubkey:'native',tags:[]};
const followed={pubkey:'followed-puppet',tags:[['proxy','https://fedi.test/1','activitypub']]};
const stranger={pubkey:'other-puppet',tags:[['fedibridge','x']]};
for(const view of ['home','global']){
  const f=ctx._tlFilter(view);
  assert.equal(f(followed),true,view+': a followed fediverse account was hidden');
  assert.equal(f(stranger),false,view+': an unfollowed fediverse account was shown');
}
assert.equal(ctx._tlFilter('global')(native),true);
assert.equal(ctx._tlFilter('home')({pubkey:'nobody',tags:[]}),false,'home shows only follows');
prefs.set('hideFediBridge',false);
assert.equal(ctx._tlFilter('global')(stranger),true,'the switch turned off shows everything');
prefs.set('hideReplies',true);
assert.equal(ctx._tlFilter('global')({pubkey:'native',tags:[],reply:true}),false);
''')
