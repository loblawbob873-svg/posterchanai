"""Run the shipped notification gate, list, toast and row against the actual q-only event."""
import json
from pathlib import Path
import subprocess
from tests.client_source import client_source, state_shim

ROOT = Path(__file__).resolve().parents[2]


def test_quote_only_event_survives_the_list_and_opens_its_own_post():
    source = client_source()
    quote = json.loads((ROOT/'tests/fixtures/nostr/ditto_quote_no_p.json').read_text())
    def segment(a,b): return source[source.index(a):source.index(b,source.index(a))]
    code = '\n'.join([
        segment('  const _quoteHits=', '  // `html` is trusted'),
        segment('  function notifList(', '  // Follows DO light'),
        # notifHtml is the last function of notifview.js (the Notifications view, split out of app.js).
        segment('  function notifHtml(', '\n  return {\n    _notifCtxHtml'),
    ])
    # _quotesMe/notifList/notifPing live in notifs.js, where app.js's live ME/LOGO read as S.ME/S.LOGO.
    code = state_shim(code) + '\n' + code
    harness = r'''
const assert=require('node:assert/strict');
const quote=QUOTE,ME={pubkey:quote.tags[0][3]},LOGO='logo';
let events=[quote], muted=false, lastToast='', lastOS, alerts=true;
const notificationAllowed=type=>{assert.equal(type,'quotes');return alerts;};
const Store={all:()=>events,get:()=>undefined},_followSeeded=false,isMutedAuthor=()=>muted;
const _reminderRows=()=>[];
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
alerts=false;lastToast='';lastOS=null;notifPing(quote);
assert.equal(lastToast,'');assert.equal(lastOS,null);assert.deepEqual(notifList(),[quote]);
alerts=true;
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
    # (watchNotifications moved to notifs.js and the view to notifview.js; both read the live ME as S.ME.)
    assert "'#p':[S.ME.pubkey], _include_quotes:true" in watch
    assert "'#p':[S.ME.pubkey], _include_quotes:true" in older


def test_a_quote_whose_q_tag_names_no_author_is_a_quote_of_you():
    """`["q", id]` with no author (NIP-18 allows it): the quoted post's own author decides, and with
    the post uncached our relay's `_include_quotes` delivery (no p tag naming you) is the answer."""
    source = client_source()
    def segment(a,b): return source[source.index(a):source.index(b,source.index(a))]
    code = '\n'.join([
        segment('  const _quoteHits=', '  // `html` is trusted'),
        segment('  function notifList(', '  // Follows DO light'),
        segment('  function notifHtml(', '\n  return {\n    _notifCtxHtml'),
    ])
    code = state_shim(code) + '\n' + code
    harness = r'''
const assert=require('node:assert/strict');
const ME={pubkey:'a'.repeat(64)},LOGO='logo',OTHER='b'.repeat(64);
const mine={id:'1'.repeat(64),pubkey:ME.pubkey,kind:1,tags:[],content:'my post',created_at:1};
const theirs={id:'2'.repeat(64),pubkey:OTHER,kind:1,tags:[],content:'x',created_at:1};
const q=(target,extra=[],id='9'.repeat(64))=>({id,pubkey:'c'.repeat(64),kind:1,created_at:5,content:'quoting',tags:[['q',target.id],...extra]});
let held=new Map([[mine.id,mine],[theirs.id,theirs]]), events=[];
const Store={all:()=>events,get:id=>held.get(id)},_followSeeded=false,isMutedAuthor=()=>false;
const _reminderRows=()=>[],notificationAllowed=()=>true;
const _notifTs=e=>e.created_at,profOf=()=>({name:'Bob'}),_tipNote=()=>null;
const isReply=()=>false,emojiName=(pk,n)=>n,enc=s=>s,_notifSaid=()=>'',_notifCtx=()=>'',timeAgo=()=>'';
const notifToast=()=>{},osNotify=()=>{},_notifCtxId=()=>'',openThread=()=>{},switchView=()=>{};
CODE
// 1. p-tagged, quoted post held: worded as a quote, not a mention.
const a=q(mine,[['p',ME.pubkey]]);
assert.equal(_quotesMe(a),true);
assert.match(notifHtml(a),/quoted your post/);
// 2. No p tag at all, quoted post held: it is still in your notifications.
const b=q(mine,[],'8'.repeat(64)); events=[b];
assert.deepEqual(notifList(),[b]); assert.match(notifHtml(b),/quoted your post/);
// 3. Somebody else's post quoted: not yours, even when it mentions you.
const c=q(theirs,[['p',ME.pubkey]],'7'.repeat(64));
assert.equal(_quotesMe(c),false); assert.match(notifHtml(c),/mentioned you/);
// 4. Quoted post NOT held, no p tag: only our relay could have delivered it under your #p.
held=new Map(); const d=q(mine,[],'6'.repeat(64));
assert.equal(_quotesMe(d),false);
_quoteHit(d); events=[d];
assert.equal(_quotesMe(d),true); assert.deepEqual(notifList(),[d]);
// ...but an event that p-tags you proves nothing about a quote.
const e=q(mine,[['p',ME.pubkey]],'5'.repeat(64)); _quoteHit(e); assert.equal(_quotesMe(e),false);
'''.replace('CODE',code)
    result=subprocess.run(['node','-e',harness],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0,result.stderr
