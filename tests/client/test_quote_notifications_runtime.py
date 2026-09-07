"""Run the shipped notification gate, list, toast and row against the actual q-only event."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_quote_only_event_survives_the_list_and_opens_its_own_post():
    source = (ROOT/'static/js/client/app.js').read_text()
    quote = json.loads((ROOT/'tests/fixtures/nostr/ditto_quote_no_p.json').read_text())
    def segment(a,b): return source[source.index(a):source.index(b,source.index(a))]
    code = '\n'.join([
        segment('  function _quotesMe(', '  // `html` is trusted'),
        segment('  function notifList(', '  // Follows DO light'),
        segment('  function notifHtml(', '  // ---------- DMs:'),
    ])
    harness = r'''
const assert=require('node:assert/strict');
const quote=QUOTE,ME={pubkey:quote.tags[0][3]},LOGO='logo';
let events=[quote], muted=false, lastToast='', lastOS;
const Store={all:()=>events},_followSeeded=false,isMutedAuthor=()=>muted;
const _notifTs=e=>e.created_at,profOf=()=>({name:'Ditto author'}),_tipNote=()=>null;
const isReply=()=>false,emojiName=(pk,n)=>n,enc=s=>s,_notifSaid=()=>'',_notifCtx=()=>'',timeAgo=()=>'';
const notifToast=s=>lastToast=s,osNotify=(t,b,o)=>lastOS={t,b,o};
const _notifCtxId=()=>'',openThread=()=>{},switchView=()=>{};
CODE
assert.deepEqual(notifList(),[quote]);
assert.match(notifHtml(quote),/quoted your post/);
assert.match(notifHtml(quote),new RegExp('data-open="'+quote.id+'"'));
notifPing(quote);
assert.match(lastToast,/quoted your post/);
assert.equal(lastOS.o.route,'post:'+quote.id);
assert.equal(lastOS.o.tag,'nostr-'+quote.id);
muted=true;assert.deepEqual(notifList(),[]);muted=false;
events=[{...quote,pubkey:ME.pubkey}];assert.deepEqual(notifList(),[]);
events=[{...quote,tags:[['q',quote.tags[0][1],'','a'.repeat(64)]]}];assert.deepEqual(notifList(),[]);
events=[{...quote,tags:[['p',ME.pubkey]]}];assert.equal(notifList().length,1);
'''.replace('QUOTE',json.dumps(quote)).replace('CODE',code)
    result=subprocess.run(['node','-e',harness],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0,result.stderr
    # Both initial subscription and older-page queries opt in; other relays retain their #p filter.
    watch=segment('  async function watchNotifications(', '  function _quotesMe(')
    older=segment('  function renderNotifications(', '  function notifGrouped(')
    assert "'#p':[ME.pubkey], _include_quotes:true" in watch
    assert "'#p':[ME.pubkey], _include_quotes:true" in older
