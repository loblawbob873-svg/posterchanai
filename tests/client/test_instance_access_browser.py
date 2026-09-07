"""Run membership navigation and welcome transitions in a real browser."""
import base64
from pathlib import Path
from contextlib import contextmanager

from tests.client.test_emoji_pack_tabs_layout import chrome

ROOT=Path(__file__).resolve().parents[2]


@contextmanager
def opened(chrome, welcome=False):
    code=(ROOT/'static/js/client/instance-access.js').read_text()
    if welcome:code+='\n'+(ROOT/'static/js/client/instance-welcome.js').read_text()
    setup=r'''
const savedStorage=new Map();Object.defineProperty(window,'localStorage',{value:{getItem:k=>savedStorage.get(k)||null,setItem:(k,v)=>savedStorage.set(k,String(v)),removeItem:k=>savedStorage.delete(k)}});
let account='a'.repeat(64), nip05='alice@example.test', qualified=true, calls=0, renders=[], held=false, replies=[], ticks=[];
const info=()=>({pubkey:account,qualified,eligible:!qualified,pending:false,address:'alice@example.test',domain:'example.test',site_name:'Example',profile_address:nip05});
const response=()=>{calls++;const data=info();return held?new Promise(r=>replies.push(()=>r({ok:true,json:async()=>data}))):Promise.resolve({ok:true,json:async()=>data});};
window.__PC_BOOTED=true;window.__PC_API_BASE__='https://example.test';
window.__PC={viewer:()=>({pubkey:account,profile:{nip05}}),standalone:()=>false,ensureAiSession:async()=>{},
 authFetch:response,enc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
 retryInstanceView:v=>renders.push(v),isView:()=>true,editOwnProfile:()=>{},signTemplate:async x=>x,LOGO:''};
window.fetch=response;window.setInterval=fn=>{ticks.push(fn);return 1;};
'''
    page='<!doctype html><meta charset="utf-8"><div id="feed"></div><script>'+setup+'</script><script>'+code+'</script>'
    target=chrome.command('Target.createTarget',{'url':'about:blank'})['targetId']
    chrome.session=chrome.command('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
    try:
        chrome.command('Page.navigate',{'url':'data:text/html;base64,'+base64.b64encode(page.encode()).decode()})
        chrome.evaluate('new Promise(r=>setTimeout(r,100))')
        yield
    finally:
        chrome.session=None;chrome.command('Target.closeTarget',{'targetId':target})


def settle(chrome):chrome.evaluate('new Promise(r=>setTimeout(r,50))')


def test_all_requested_apps_block_until_verified_then_allow_without_repeat_signin(chrome):
    with opened(chrome):
        expected=['news','meme','mail','office','sync','vault','torrents','analytics','media-center','repos','texts','notes','wallet','exodus','websearch']
        assert chrome.evaluate('Object.keys(PCInstanceAccess.apps)')==expected
        assert chrome.evaluate('Object.keys(PCInstanceAccess.apps).every(v=>!PCInstanceAccess.allowed(v))')
        assert chrome.evaluate("PCInstanceAccess.allowed('messages') && PCInstanceAccess.allowed('terminal')")
        assert chrome.evaluate("PCInstanceAccess.gate('wallet')")
        settle(chrome)
        assert chrome.evaluate('Object.keys(PCInstanceAccess.apps).every(v=>PCInstanceAccess.allowed(v))')
        assert chrome.evaluate('renders')==['wallet']
        assert chrome.evaluate("PCInstanceAccess.gate('wallet')") is False
        assert chrome.evaluate('calls')==1


def test_other_account_reply_and_profile_change_cannot_reuse_grant(chrome):
    with opened(chrome):
        chrome.evaluate('held=true;void PCInstanceAccess.refresh()');settle(chrome)
        chrome.evaluate("account='b'.repeat(64);PCInstanceAccess.allowed('vault');replies.shift()()")
        settle(chrome)
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is False
        chrome.evaluate('held=false;PCInstanceAccess.refresh(true)')
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is True
        chrome.evaluate("nip05='someone@elsewhere.test'")
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is False
        chrome.evaluate("PCInstanceAccess.accept({...info(),pubkey:'a'.repeat(64)},account)")
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is False


def test_denied_or_unavailable_is_actionable_without_request_loop(chrome):
    with opened(chrome):
        chrome.evaluate("qualified=false;PCInstanceAccess.gate('notes')");settle(chrome)
        assert chrome.evaluate('calls')==1
        assert chrome.evaluate("document.querySelector('#feed').textContent.includes('Edit profile')")
        assert chrome.evaluate("PCInstanceAccess.allowed('notes')") is False
        chrome.evaluate("__PC.authFetch=async()=>{throw Error('offline')};document.querySelector('.ia-retry').click()")
        settle(chrome)
        assert 'offline' in chrome.evaluate("document.querySelector('.ia-status').textContent")
        assert chrome.evaluate("PCInstanceAccess.allowed('notes')") is False


def test_welcome_stays_hidden_for_member_and_returns_only_after_profile_change(chrome):
    with opened(chrome,welcome=True):
        assert chrome.evaluate("document.querySelector('dialog')===null")
        before=chrome.evaluate('calls')
        chrome.evaluate('ticks.forEach(fn=>fn())');settle(chrome)
        assert chrome.evaluate('calls')==before
        chrome.evaluate("nip05='alice@elsewhere.test';qualified=false;ticks.forEach(fn=>fn())")
        settle(chrome)
        assert chrome.evaluate("!!document.querySelector('dialog[open]')")
        chrome.evaluate("nip05='alice@example.test';qualified=true;ticks.forEach(fn=>fn())")
        settle(chrome)
        assert chrome.evaluate("document.querySelector('dialog')===null")
        before=chrome.evaluate('calls')
        chrome.evaluate('ticks.forEach(fn=>fn())');settle(chrome)
        assert chrome.evaluate('calls')==before
        chrome.evaluate("account='b'.repeat(64);nip05='';qualified=false;ticks.forEach(fn=>fn())")
        settle(chrome)
        assert chrome.evaluate("!!document.querySelector('dialog[open]')")


def test_profile_edit_forces_server_cache_refresh_and_retries_after_outage(chrome):
    with opened(chrome):
        chrome.evaluate("window.urls=[];__PC.authFetch=async url=>{urls.push(url);return {ok:true,json:async()=>({...info(),qualified:!url.includes('refresh=1')})}};PCInstanceAccess.refresh()")
        assert chrome.evaluate("PCInstanceAccess.allowed('websearch')")
        chrome.evaluate("nip05='elsewhere@example.org';PCInstanceAccess.refresh()")
        assert chrome.evaluate("PCInstanceAccess.allowed('websearch')") is False
        assert chrome.evaluate('urls')==['/api/instance-welcome/access','/api/instance-welcome/access?refresh=1']
        chrome.evaluate("nip05='alice@example.test';__PC.authFetch=async()=>{throw Error('offline')};PCInstanceAccess.refresh()")
        chrome.evaluate("__PC.authFetch=async url=>{urls.push(url);return {ok:true,json:async()=>info()}};PCInstanceAccess.refresh()")
        assert chrome.evaluate('urls.at(-1)')=='/api/instance-welcome/access?refresh=1'
        assert chrome.evaluate("PCInstanceAccess.allowed('websearch')")


def test_verified_local_apps_survive_offline_reload_but_not_profile_or_account_change(chrome):
    import json
    code=(ROOT/'static/js/client/instance-access.js').read_text()
    with opened(chrome):
        chrome.evaluate('PCInstanceAccess.refresh()')
        chrome.evaluate("Object.defineProperty(navigator,'onLine',{value:false,configurable:true})")
        chrome.evaluate('eval('+json.dumps(code)+')')
        assert chrome.evaluate("PCInstanceAccess.allowed('notes') && PCInstanceAccess.allowed('vault') && PCInstanceAccess.allowed('texts')")
        assert chrome.evaluate("PCInstanceAccess.allowed('wallet') || PCInstanceAccess.allowed('websearch')") is False
        chrome.evaluate("PCInstanceAccess.require('vault')")
        chrome.evaluate("nip05='alice@elsewhere.test'")
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is False
        chrome.evaluate("nip05='alice@example.test'")
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is False
        chrome.evaluate("account='b'.repeat(64)")
        assert chrome.evaluate("PCInstanceAccess.allowed('notes')") is False


def test_online_revocation_removes_previously_verified_offline_access(chrome):
    with opened(chrome):
        chrome.evaluate('PCInstanceAccess.refresh()')
        chrome.evaluate('qualified=false;PCInstanceAccess.refresh(true)')
        chrome.evaluate("Object.defineProperty(navigator,'onLine',{value:false,configurable:true})")
        assert chrome.evaluate("PCInstanceAccess.allowed('notes')") is False
        assert chrome.evaluate("PCInstanceAccess.refresh().then(x=>x===null)")


def test_late_profile_hydration_preserves_verified_offline_membership(chrome):
    import json
    code=(ROOT/'static/js/client/instance-access.js').read_text()
    with opened(chrome):
        chrome.evaluate('PCInstanceAccess.refresh()')
        chrome.evaluate("window.profileReady=false;__PC.viewer=()=>({pubkey:account,profile:profileReady?{nip05}:{},profileKnown:profileReady});Object.defineProperty(navigator,'onLine',{value:false,configurable:true})")
        chrome.evaluate('eval('+json.dumps(code)+')')
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is False
        chrome.evaluate('profileReady=true')
        assert chrome.evaluate("PCInstanceAccess.allowed('vault')") is True
