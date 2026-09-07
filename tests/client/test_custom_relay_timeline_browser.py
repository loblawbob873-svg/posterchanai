"""Reconnect the real relay pool and run its actual follow/mute hydration in Chromium."""
import base64
import json
import os
from pathlib import Path

import pytest
from tests.client.test_emoji_pack_tabs_layout import chrome

ROOT = Path(__file__).resolve().parents[2]


def _page(initial_view):
    app = Path(os.environ.get('PC_RECONNECT_APP', ROOT / 'static/js/client/app.js')).read_text()
    start = app.find('  function _sameMembers(')
    follows = app[start if start >= 0 else app.index('  async function fetchFollows('):app.index('  // Automatic filtering has its own')]
    mutes = app[app.index('  async function fetchMutes('):app.index('  // Replace the `word` tags')]
    reconnect = app[app.index('    const _isTyping = ()=>{'):app.index('    connectRelays();', app.index('    const _isTyping = ()=>{'))]
    lists = app[app.index('  async function fetchPins('):app.index('  async function _editEList(')] + app[app.index('  async function fetchBookmarks('):app.index('  async function toggleBookmark(')]
    anchors = app[app.index('  function _tlNotes('):app.index('  /* Restore a history entry by POST,') ]
    timeline = app[app.index('  function renderTimeline('):app.index('  // Batched live updates:')]
    draw = app[app.index('  function _drawTimeline(preserveScroll){'):app.index('  // ---------- infinite scroll-back ----------')]
    vendor = (ROOT / 'static/vendor/nostr/nostr.bundle.js').read_text()
    relay = (ROOT / 'static/js/client/relay.js').read_text()
    script = r'''
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const pub=n=>NostrTools.getPublicKey(Uint8Array.from({length:32},(_,i)=>i===31?n:0));
let ME={pubkey:pub(1)},GUEST=false,VIEW='home',_tlGen=1;
let FOLLOWS=new Set([pub(1),pub(2)]),MUTED=new Set([pub(3)]),MUTED_WORDS=new Set(),MUTED_THREADS=new Set(),PINNED=new Set(['pinned']),BOOKMARKS=new Set(['saved']);
const ClientSettings={get:(_k,d)=>d,set:()=>{}},_followSafetyMembers=()=>[],_persistFollows=()=>{},_persistMutes=()=>{};
const _syncAutoMutes=()=>Promise.resolve(),NO_IMAGES=true,needProfile=()=>{},toast=()=>{};
let _followShrinkWarned=false,resetCalls=0,subscriptionChanges=0,otherCalls=0;
const fetchMyProfile=async()=>{},_reaskMissing=()=>{},decorateCounts=()=>{},renderBookmarks=()=>{};
const _cacheFirstList=async(filters,apply)=>{if(filters[0].kinds[0]===10001)otherCalls++;apply(await Relay.query(filters));};
let _tl={pages:3,oldest:77,eosed:true},_tlForceTop='',_liveSince=0,_tlPause=null,_tlResume=null,_tlPaused=false,_tlPausedAt=0;
const _tlScrollMemo={},subs={},_TL_KINDS=[1],_FEED_MAX_CARDS=400,_tlMedia=false;
const _resetLive=()=>{},_putScroll=top=>requestAnimationFrame(()=>$('#feed').scrollTop=top);
const timelineFilter=()=>[{kinds:[1],authors:[...FOLLOWS]}];
const notes=Array.from({length:12},(_,i)=>({id:'post'+i,pubkey:pub(i===4?3:2),created_at:100-i}));
const Store={feed:fn=>notes.filter(fn)},_tlFilter=()=>()=>true,isMutedView=e=>MUTED.has(e.pubkey),_noteKey=e=>e.id;
const _noteNode=e=>{const n=document.createElement('article');n.className='note';n.dataset.key=e.id;n.textContent=e.id;return n;};
const _profObs=null,_skelNotes=()=>'<div class=loading></div>';
const _healGhostPairs=()=>{},hydrate=()=>{},_bindTimelineHeader=()=>{},_timelineHeaderHtml=()=>'<textarea id="tl-cmp-ta"></textarea>';

// Model renderView's destructive reset explicitly; the production hydration must never call it.
function renderView(reset){if(reset){resetCalls++;$('#feed').innerHTML='<div class="spinner"></div>';} _drawTimeline(true);}

window.rows={};window.held=[];window.hold=false;window.requests=[];
function signed(kind,tags){return NostrTools.finalizeEvent({kind,tags,content:'',created_at:Math.floor(Date.now()/1000)},Uint8Array.from({length:32},(_,i)=>i===31?1:0));}
function setRows(follows=[pub(2)],mutes=[pub(3)]){rows[3]=signed(3,follows.map(p=>['p',p]));rows[10000]=signed(10000,mutes.map(p=>['p',p]));}
setRows();rows[10001]=signed(10001,[['e','pinned']]);rows[10003]=signed(10003,[['e','saved']]);
class FakeWorker {
 postMessage(m){queueMicrotask(()=>this.onmessage({data:{id:m.id,ok:true,data:m.args.events.map(e=>({id:e.id,valid:NostrTools.verifyEvent(e)}))}}));}
}
window.Worker=FakeWorker;
class FakeSocket {
 static sockets=[];
 constructor(url){this.url=url;this.readyState=0;FakeSocket.sockets.push(this);}
 send(raw){const m=JSON.parse(raw);if(m[0]!=='REQ')return;requests.push(m);
   const row=rows[m[2].kinds[0]];
   const reply=()=>{if(this.readyState!==1)return;if(row)this.onmessage({data:JSON.stringify(['EVENT',m[1],row])});this.onmessage({data:JSON.stringify(['EOSE',m[1]])});};
   if(hold)held.push(reply);else queueMicrotask(reply);
 }
 open(){this.readyState=1;this.onopen();}
 close(){this.readyState=3;}
}
window.WebSocket=FakeSocket;
'''
    script=script.replace("VIEW='home'",'VIEW='+json.dumps(initial_view),1)
    checks = r'''
function reconnectFlapper(){const ws=FakeSocket.sockets[1];if(ws.readyState===1){ws.close();ws.onclose();clearTimeout(Relay._conns.get(ws.url)._rt);}ws.open();}
const realRenderTimeline=renderTimeline;renderTimeline=(...args)=>{subscriptionChanges++;try{return realRenderTimeline(...args);}catch(e){fixtureErrors.push(String(e));throw e;}};
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setTimeout(r,5));};
Relay.configure({urls:['wss://healthy.example','wss://flapping.example','wss://unreachable.example'],verify:true});
FakeSocket.sockets[0].open();
subs[VIEW]=Relay.subscribe(timelineFilter(),{onEvent:()=>{},onEose:()=>{}});
_drawTimeline(true);$('#feed').scrollTop=600;
const card=$('#tl-notes').children[2],composer=$('#tl-cmp-ta'),cursor=_tl.oldest,anchorBefore=_tlAnchor($('#feed'));
window.runCase=async name=>{
 if(name==='unchanged'){
  for(let i=0;i<5;i++){reconnectFlapper();await settle();}
  return {resetCalls,sameCard:card.isConnected,sameComposer:composer===$('#tl-cmp-ta'),top:$('#feed').scrollTop,cursor:_tl.oldest,pages:_tl.pages,subscriptionChanges};
 }
 if(name==='changed'){
  setRows([pub(2),pub(4)],[pub(2),pub(3)]);reconnectFlapper();await settle();
  return {resetCalls,subscriptionChanges,follows:FOLLOWS.has(pub(4)),muted:MUTED.has(pub(2)),authorRequested:requests.some(m=>m[2].kinds[0]===1&&m[2].authors.includes(pub(4))),sameComposer:composer===$('#tl-cmp-ta')};
 }
 if(name==='overlap'){
  hold=true;reconnectFlapper();await settle();
  for(let i=0;i<20;i++)Relay._connReady();
  const during=otherCalls;hold=false;held.splice(0).forEach(f=>f());await settle();
  return {during,after:otherCalls,resetCalls,sameCard:card.isConnected};
 }
 if(name==='typing'||name==='typing_changed'){
  if(name==='typing_changed')setRows([pub(2),pub(4)]);
  composer.value='unfinished post';composer.focus();composer.setSelectionRange(2,8);
  reconnectFlapper();await settle();
  return {resetCalls,sameComposer:composer===$('#tl-cmp-ta'),text:composer.value,focused:document.activeElement===composer,selection:[composer.selectionStart,composer.selectionEnd],subscriptionChanges};
 }
 if(name==='navigate'){
  hold=true;reconnectFlapper();await settle();VIEW='thread';_tlGen++;
  hold=false;held.splice(0).forEach(f=>f());await settle();return {resetCalls,view:VIEW,sameCard:card.isConnected};
 }
 if(name==='account'){
  hold=true;reconnectFlapper();await settle();ME={pubkey:pub(4)};FOLLOWS=new Set([pub(4)]);MUTED=new Set();PINNED=new Set();BOOKMARKS=new Set();
  hold=false;held.splice(0).forEach(f=>f());await settle();return {resetCalls,follows:[...FOLLOWS],mutes:[...MUTED],pins:[...PINNED],bookmarks:[...BOOKMARKS],owner:pub(4)};
 }
 if(name==='mute_only'){
  setRows([pub(2)],[pub(2),pub(3)]);reconnectFlapper();await settle();
  return {resetCalls,subscriptionChanges,muted:MUTED.has(pub(2)),pages:_tl.pages,sameComposer:composer===$('#tl-cmp-ta')};
 }
 if(name==='clear_mutes'){
  setRows([pub(2)],[]);reconnectFlapper();await settle();
  return {resetCalls,subscriptionChanges,mutes:MUTED.size,anchorBefore,anchorAfter:_tlAnchor($('#feed')),sameCard:card.isConnected};
 }
 if(name==='reordered'){
  setRows([pub(2),pub(1)]);reconnectFlapper();await settle();
  return {resetCalls,subscriptionChanges,sameCard:card.isConnected};
 }
 if(name==='empty'){
  rows={};reconnectFlapper();await settle();return {resetCalls,sameCard:card.isConnected,kept:FOLLOWS.has(pub(2))&&MUTED.has(pub(3)),subscriptionChanges};
 }
};
'''
    return ('<!doctype html><script>window.fixtureErrors=[];window.onerror=m=>fixtureErrors.push(String(m));</script><style>#feed{height:240px;overflow:auto}.note{height:120px}</style>'
            '<main id="feed"><textarea id="tl-cmp-ta"></textarea><div id="tl-notes"></div></main>'
            + ''.join('<script>'+part+'</script>' for part in [vendor,script,relay,follows,mutes,lists,anchors,draw,timeline,reconnect,checks]))


@pytest.mark.parametrize('initial_view',['home','global'])
@pytest.mark.parametrize('case',['unchanged','changed','overlap','typing','navigate','account','empty','typing_changed','mute_only','reordered','clear_mutes'])
def test_custom_relay_recovery_keeps_timeline_stable(chrome,case,initial_view):
    target=chrome.command('Target.createTarget',{'url':'about:blank'})['targetId']
    chrome.session=chrome.command('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
    try:
        chrome.command('Page.navigate',{'url':'data:text/html;base64,'+base64.b64encode(_page(initial_view).encode()).decode()})
        chrome.evaluate('new Promise(r=>setTimeout(r,150))')
        assert chrome.evaluate('fixtureErrors')==[],chrome.evaluate('fixtureErrors')
        got=chrome.evaluate('runCase('+json.dumps(case)+')')
        assert chrome.evaluate('fixtureErrors')==[],chrome.evaluate('fixtureErrors')
        assert got['resetCalls']==0,got
        if case=='unchanged':
            assert got==dict(resetCalls=0,sameCard=True,sameComposer=True,top=600,cursor=77,pages=3,subscriptionChanges=0)
        elif case=='changed':
            assert got['subscriptionChanges']==int(initial_view=='home') and got['follows'] and got['muted'] and got['sameComposer'] and got['authorRequested']==(initial_view=='home'),got
        elif case=='overlap':
            assert got['during']==1 and got['after']==2 and got['sameCard'],got
        elif case in ('typing','typing_changed'):
            assert got['sameComposer'] and got['text']=='unfinished post' and got['focused'] and got['selection']==[2,8],got
            assert got['subscriptionChanges']==int(case=='typing_changed' and initial_view=='home'),got
        elif case=='navigate':
            assert got['view']=='thread' and got['sameCard'],got
        elif case=='account':
            assert got['follows']==[got['owner']] and got['mutes']==[] and got['pins']==[] and got['bookmarks']==[],got
        elif case=='mute_only':
            assert got['subscriptionChanges']==0 and got['muted'] and got['pages']==3 and got['sameComposer'],got
        elif case=='clear_mutes':
            assert got['subscriptionChanges']==0 and got['mutes']==0 and got['sameCard'] and got['anchorBefore']==got['anchorAfter'],got
        elif case=='reordered':
            assert got['subscriptionChanges']==0 and got['sameCard'],got
        elif case=='empty':
            assert got['sameCard'] and got['kept'] and got['subscriptionChanges']==0,got
    finally:
        chrome.session=None
        chrome.command('Target.closeTarget',{'targetId':target})
