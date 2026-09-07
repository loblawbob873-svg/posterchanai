"""Actual settings handlers, signed lists, browser storage/reload and intact DM drafts."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json

import pytest
from tests.client.test_emoji_pack_tabs_layout import chrome

ROOT = Path(__file__).resolve().parents[2]


def document():
    app = (ROOT / 'static/js/client/app.js').read_text()
    controller = app[app.index('  // Automatic filtering has its own account-scoped cache.'):
                     app.index('  async function fetchMutes(){')]
    toggle = app[app.index('  async function toggleMute(pk){'):app.index('  async function fetchPins()')]
    controls = app[app.index("    { const toggle=$('#set-auto-mute'), update=$('#set-auto-mute-update');"):
                   app.index("    { const wb=$('#set-words-save');")]
    start = app.index('<div class="us-pane" data-pane="muted">')
    end = app.index('Blur sensitive / NSFW posts', start)
    panel = app[start:end]
    panel = panel[:panel.rfind('<label class="fld"')].replace('class="us-pane"', 'class="us-pane active"') + '</div>'
    timeline = app[app.index('  function _drawTimeline(preserveScroll){'):app.index('  // ---------- infinite scroll-back ----------')]
    rows = app[app.index('  function _renderDmPeerRows('):app.index('  function renderMessages(){')]
    recount = app[app.index('  function recountDmUnread()'):app.index('  function _dmNotify(')]
    css = (ROOT / 'static/css/client.css').read_text()
    vendor = (ROOT / 'static/vendor/nostr/nostr.bundle.js').read_text()
    engine = (ROOT / 'static/js/client/auto-mute.js').read_text()
    script = r'''
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const NT=()=>NostrTools;
const key=n=>Uint8Array.from({length:32},(_,i)=>i===31?n:0), pub=n=>NT().getPublicKey(key(n));
let ME={pubkey:pub(1)}, GUEST=false, MUTED=new Set([pub(3)]), VIEW='messages', dmActive=pub(2);
let _dmUnread=0, recounts=0, notifBumps=0, renderCalls=0, manualWrites=0;
const dmPeers=new Map([[pub(2),[{mine:false,t:100}]], [pub(3),[{mine:false,t:100}]]]);
const ClientSettings={get:()=>0}, bumpDm=()=>recounts++, bumpNotif=()=>notifBumps++;
const renderView=()=>{renderCalls++;document.querySelector('#dm-in')?.remove();};
const enc=s=>String(s||''), profOf=()=>({name:'Peer'}), needProfile=()=>{}, niceNip05=()=>'', LOGO='', emojiName=(_pk,n)=>n, _dmWhen=()=>'', openDm=()=>{};
const _tl={pages:0,eosed:true}, _FEED_MAX_CARDS=400, _tlMedia=false, _profObs=null;
const _tlAnchor=()=>null, _restoreTlAnchor=()=>false, _tlFilter=()=>()=>true, _healGhostPairs=()=>{}, hydrate=()=>{};
const Store={feed:fn=>[{id:'peer',pubkey:pub(2),created_at:2},{id:'other',pubkey:pub(4),created_at:1}].filter(fn)};
const isMutedView=e=>isMutedAuthor(e.pubkey), _noteKey=e=>e.id;
const _noteNode=e=>{const n=document.createElement('article');n.className='note';n.dataset.key=e.id;n.dataset.pk=e.pubkey;n.innerHTML='<video></video>';return n;};
let moduleHold=false,modulePending=[];
const _withModule=()=>moduleHold?new Promise(resolve=>modulePending.push(resolve)):Promise.resolve(PCAutoMute), toast=()=>{}, _persistMutes=()=>{};
const _editPList=async()=>{manualWrites++;return true;};
let peerMuted=true, timestamp=100, hold=false, pending=[], queryCalls=0;
const answer=filters=>{
  const event=NT().finalizeEvent({kind:10000,created_at:timestamp,
    tags:peerMuted?[['p',ME.pubkey]]:[],content:''},key(2));
  const rows=filters[0]['#p']?(peerMuted?[event]:[]):[event];
  return Object.defineProperty(rows,'complete',{value:!new URL(location.href).searchParams.has('offline')});
};
const Relay={worker:{call:async(_method,{event})=>({valid:NT().verifyEvent(event)})},query:filters=>{queryCalls++;return hold?new Promise(resolve=>pending.push(()=>resolve(answer(filters)))):Promise.resolve(answer(filters));}};
''' + controller + toggle + recount + rows + timeline + r'''
document.querySelector('#user-settings').innerHTML=`PANEL`;
document.querySelector('#dm-list').innerHTML='<input id="dm-search"><div id="dm-rows"></div>';
_renderDmPeerRows();
document.querySelector('#feed-note').dataset.pk=pub(2);
const originalInput=document.querySelector('#dm-in'), originalAttachment=document.querySelector('#dm-atts');
originalInput.value='Keep this unsent draft';originalInput.setSelectionRange(2,7);
''' .replace('PANEL', panel) + controls + r'''
_syncAutoMutes().catch(()=>{});
window.booted=true;
'''
    return '<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1"><style>' + css + '''
body{zoom:1!important;display:block!important;margin:0;padding:8px;box-sizing:border-box}
#user-settings{max-width:760px}.us-pane{display:block!important}#dm-list{display:block!important}
</style><body><section id="user-settings"></section><article class="note" id="feed-note">A post</article>
<div id="dm-list"></div><div id="dm-thread"><div class="topbar"><button id="dm-mute">Mute</button></div>
<div id="dm-atts">An unsent attachment</div><textarea id="dm-in"></textarea></div>
<script>''' + vendor + '</script><script>' + engine + '</script><script>' + script + '</script></body></html>'


@contextmanager
def served():
    payload = document().encode()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def opened(chrome, width):
    with served() as url:
        target = chrome.command('Target.createTarget', {'url':'about:blank'})['targetId']
        chrome.session = chrome.command('Target.attachToTarget', {'targetId':target,'flatten':True})['sessionId']
        try:
            chrome.command('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':False})
            chrome.command('Emulation.setTouchEmulationEnabled', {'enabled':width<600})
            chrome.command('Page.enable')
            chrome.command('Page.navigate', {'url':url})
            chrome.evaluate('new Promise(resolve=>setTimeout(resolve,300))')
            assert chrome.evaluate('window.booted')
            yield url
        finally:
            chrome.session=None
            chrome.command('Target.closeTarget', {'targetId':target})


def settle(chrome):
    chrome.evaluate('new Promise(resolve=>setTimeout(resolve,100))')


@pytest.mark.parametrize('width',[320,375,1280])
def test_settings_touch_reload_offline_hydration_and_manual_mutes(chrome,width):
    with opened(chrome,width) as url:
        assert chrome.evaluate('queryCalls')==0
        assert chrome.evaluate("document.querySelector('#set-auto-mute-update').disabled")
        chrome.click('.switch:has(#set-auto-mute)',touch=width<600)
        settle(chrome)
        assert chrome.evaluate('_autoMuteHas(pub(2)) && isMutedAuthor(pub(3))')
        assert chrome.evaluate("document.querySelector('#feed-note').classList.contains('auto-muted-by-peer')")
        assert chrome.evaluate('originalInput===document.querySelector("#dm-in") && originalAttachment.isConnected && originalInput.value==="Keep this unsent draft" && originalInput.selectionStart===2 && originalInput.selectionEnd===7')
        assert chrome.evaluate('renderCalls===0 && manualWrites===0 && _dmUnread===0')
        # A check that changes nothing must not touch the composer or badge counts.
        chrome.evaluate('window.countBefore=recounts; document.querySelector("#dm-in").focus(); _updateAutoMutes()')
        assert chrome.evaluate('recounts===countBefore && document.activeElement===originalInput && renderCalls===0')
        chrome.command('Page.navigate',{'url':url+'?offline=1'})
        settle(chrome)
        assert chrome.evaluate('window.booted && _autoMuteHas(pub(2)) && isMutedAuthor(pub(3))')
        assert chrome.evaluate("document.querySelector('#set-auto-mute').checked")
        assert chrome.evaluate("document.querySelector('#set-auto-mute-status').textContent.includes('Last checked')")
        chrome.click('#set-auto-mute-update',touch=width<600)
        settle(chrome)
        assert chrome.evaluate('_autoMuteHas(pub(2)) && isMutedAuthor(pub(3))')
        assert chrome.evaluate("document.querySelector('#set-auto-mute-status').textContent.includes('incomplete')")
        chrome.command('Page.navigate',{'url':url})
        settle(chrome)
        chrome.evaluate('peerMuted=false;timestamp=101')
        chrome.click('#set-auto-mute-update',touch=width<600)
        settle(chrome)
        assert chrome.evaluate('!_autoMuteHas(pub(2)) && isMutedAuthor(pub(3)) && manualWrites===0 && renderCalls===0')
        assert chrome.evaluate("document.querySelector('#set-auto-mute-status').textContent.includes('1 removed')")
        assert chrome.evaluate('originalInput===document.querySelector("#dm-in") && originalAttachment.isConnected')


def test_pending_check_disable_reenable_starts_fresh_and_keeps_draft(chrome):
    with opened(chrome,375):
        chrome.evaluate('hold=true')
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        assert chrome.evaluate('pending.length')==1
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        chrome.evaluate('hold=false')
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        assert chrome.evaluate('_autoMuteHas(pub(2))')
        chrome.evaluate('pending.splice(0).forEach(resolve=>resolve())')
        settle(chrome)
        assert chrome.evaluate('_autoMuteHas(pub(2)) && renderCalls===0')
        assert chrome.evaluate('!document.querySelector("#set-auto-mute-status").textContent.includes("cancelled")')


def test_pending_account_switch_updates_only_new_account(chrome):
    with opened(chrome,1280):
        chrome.evaluate('hold=true')
        chrome.click('.switch:has(#set-auto-mute)')
        settle(chrome)
        chrome.evaluate("localStorage.setItem('pc_auto_mute:'+pub(4),JSON.stringify({owner:pub(4),enabled:true,records:{},ignored:[]}));ME={pubkey:pub(4)};hold=false;_syncAutoMutes()")
        chrome.evaluate('_paintAutoMuteControls()')
        assert not chrome.evaluate('document.querySelector("#set-auto-mute-update").disabled')
        chrome.click('#set-auto-mute-update')
        settle(chrome)
        assert chrome.evaluate('_autoMuteHas(pub(2))')
        chrome.evaluate('pending.splice(0).forEach(resolve=>resolve())')
        settle(chrome)
        assert chrome.evaluate("Object.keys(JSON.parse(localStorage.getItem('pc_auto_mute:'+pub(1))).records).length===0")
        assert chrome.evaluate("JSON.parse(localStorage.getItem('pc_auto_mute:'+pub(4))).records[pub(2)].muted===true")


def test_manual_auto_unmute_exception_and_disabled_setting_persist(chrome):
    with opened(chrome,375) as url:
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        chrome.evaluate('toggleMute(pub(2))')
        assert chrome.evaluate('!isMutedAuthor(pub(2)) && isMutedAuthor(pub(3)) && manualWrites===0')
        chrome.command('Page.reload')
        settle(chrome)
        chrome.evaluate('_updateAutoMutes()')
        assert chrome.evaluate('!isMutedAuthor(pub(2)) && isMutedAuthor(pub(3)) && manualWrites===0')
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        chrome.command('Page.reload')
        settle(chrome)
        assert chrome.evaluate('!document.querySelector("#set-auto-mute").checked && queryCalls===0')
        assert chrome.evaluate('isMutedAuthor(pub(3)) && !isMutedAuthor(pub(2))')


def test_background_reconciliation_preserves_focused_dm_draft_and_attachment(chrome):
    with opened(chrome,1280):
        chrome.click('.switch:has(#set-auto-mute)')
        settle(chrome)
        chrome.evaluate('originalInput.focus();originalInput.setSelectionRange(3,8);peerMuted=false;timestamp=101;_updateAutoMutes()')
        assert chrome.evaluate('document.activeElement===originalInput && originalInput.selectionStart===3 && originalInput.selectionEnd===8')
        assert chrome.evaluate('originalAttachment.isConnected && renderCalls===0 && manualWrites===0')
        assert chrome.evaluate('!_autoMuteHas(pub(2)) && isMutedAuthor(pub(3)) && _dmUnread===1')


def test_cached_hidden_conversation_returns_on_remote_unmute_without_remount(chrome):
    with opened(chrome,375) as url:
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        chrome.command('Page.reload')
        settle(chrome)
        assert chrome.evaluate('_autoMuteHas(pub(2)) && !document.querySelector(".dm-peer")')
        chrome.evaluate('originalInput.focus();originalInput.setSelectionRange(4,9);peerMuted=false;timestamp=101;_updateAutoMutes()')
        assert chrome.evaluate('document.querySelector(".dm-peer").dataset.peer===pub(2)')
        assert chrome.evaluate('document.activeElement===originalInput && originalInput.selectionStart===4 && originalInput.selectionEnd===9 && originalAttachment.isConnected && renderCalls===0')
        assert chrome.evaluate('isMutedAuthor(pub(3)) && manualWrites===0')


def test_cached_hidden_timeline_post_returns_without_replacing_other_media(chrome):
    with opened(chrome,1280):
        chrome.click('.switch:has(#set-auto-mute)')
        settle(chrome)
        chrome.command('Page.reload')
        settle(chrome)
        chrome.evaluate("VIEW='home';document.body.insertAdjacentHTML('beforeend','<section id=feed><textarea id=timeline-draft></textarea><div id=tl-notes></div></section>');_drawTimeline(true);window.keptVideo=document.querySelector('#tl-notes video');window.keptDraft=document.querySelector('#timeline-draft');keptDraft.value='Keep my post';keptDraft.focus()")
        assert chrome.evaluate('!document.querySelector("[data-key=peer]") && !!keptVideo')
        chrome.evaluate('peerMuted=false;timestamp=101;_updateAutoMutes()')
        assert chrome.evaluate('!!document.querySelector("[data-key=peer]") && keptVideo===document.querySelector("[data-key=other] video")')
        assert chrome.evaluate('document.activeElement===keptDraft && keptDraft.value==="Keep my post"')


def test_another_tab_hydrates_saved_enable_disable_changes(chrome):
    with opened(chrome,1280) as url:
        chrome.click('.switch:has(#set-auto-mute)')
        settle(chrome)
        first_session=chrome.session
        target=chrome.command('Target.createTarget',{'url':'about:blank'})['targetId']
        other_session=chrome.command('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
        try:
            chrome.session=other_session
            chrome.command('Page.enable')
            chrome.command('Page.navigate',{'url':url})
            settle(chrome)
            assert chrome.evaluate('_autoMuteHas(pub(2))')
            chrome.evaluate('_loadAutoMute().then(engine=>engine.setEnabled(false))')
            chrome.session=first_session
            settle(chrome)
            assert chrome.evaluate('!_autoMuteHas(pub(2)) && !document.querySelector("#set-auto-mute").checked && isMutedAuthor(pub(3))')
            assert chrome.evaluate('originalInput.isConnected && originalAttachment.isConnected && renderCalls===0')
            chrome.session=other_session
            chrome.evaluate('_loadAutoMute().then(engine=>engine.setEnabled(true))')
            chrome.session=first_session
            settle(chrome)
            assert chrome.evaluate('_autoMuteHas(pub(2)) && document.querySelector("#set-auto-mute").checked')
        finally:
            chrome.session=None
            chrome.command('Target.closeTarget',{'targetId':target})
            chrome.session=first_session


def test_toggle_persists_before_lazy_load_and_survives_reload(chrome):
    with opened(chrome,375) as url:
        chrome.evaluate("moduleHold=true;const t=document.querySelector('#set-auto-mute');t.checked=true;void t.onchange()")
        assert chrome.evaluate("JSON.parse(localStorage.getItem(_autoMuteStoreKey())).enabled===true")
        assert chrome.evaluate("modulePending.length===1")
        chrome.command('Page.navigate',{'url':url+'?offline=1'})
        settle(chrome)
        assert chrome.evaluate("document.querySelector('#set-auto-mute').checked")
        assert chrome.evaluate("_autoMuteStored().enabled===true")


def test_delayed_module_rapid_toggle_preserves_latest_choice_and_records(chrome):
    with opened(chrome,375):
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        chrome.evaluate("const beforeRecord=JSON.stringify(_autoMuteStored().records);moduleHold=true;const t=document.querySelector('#set-auto-mute');t.checked=false;void t.onchange();t.checked=true;void t.onchange();")
        assert chrome.evaluate("JSON.parse(localStorage.getItem(_autoMuteStoreKey())).enabled===true")
        assert chrome.evaluate("JSON.stringify(_autoMuteStored().records)===beforeRecord")
        assert chrome.evaluate("_autoMuteHas(pub(2))")
        chrome.evaluate("moduleHold=false;modulePending.splice(0).reverse().forEach(resolve=>resolve(PCAutoMute))")
        settle(chrome)
        assert chrome.evaluate("document.querySelector('#set-auto-mute').checked && _autoMuteStored().enabled===true")
        assert chrome.evaluate("JSON.stringify(_autoMuteStored().records)===beforeRecord")


def test_failed_toggle_write_keeps_prior_filter_and_shows_error(chrome):
    with opened(chrome,375):
        chrome.click('.switch:has(#set-auto-mute)',touch=True)
        settle(chrome)
        chrome.evaluate("const saved=localStorage.getItem(_autoMuteStoreKey());const originalSet=Storage.prototype.setItem;Storage.prototype.setItem=function(){throw Error('storage full')};const t=document.querySelector('#set-auto-mute');t.checked=false;void t.onchange();")
        settle(chrome)
        assert chrome.evaluate("document.querySelector('#set-auto-mute').checked && _autoMuteHas(pub(2))")
        assert chrome.evaluate("localStorage.getItem(_autoMuteStoreKey())===saved")
        assert chrome.evaluate("document.querySelector('#set-auto-mute-status').textContent.includes('storage full')")
        chrome.evaluate('Storage.prototype.setItem=originalSet')
