"""Settings: the last fourteen buttons no test pressed, each pressed in the SHIPPED bundle and checked for what it says
it does (2026-10-22: every button gets a test).

  * Account card: '#set-admin' opens the Admin panel (admins only); '#set-google-link' (confirmed) uploads THIS key's
    nsec with a signed 27235 to /api/auth/google/link/start and goes to the auth_url it is handed, or says why not;
    '#set-del-account' (confirmed) posts a signed 27235 for YOUR pubkey to /client/delete-account -- a refusal is
    said and keeps you signed in, an ok signs you out.
  * User settings: '#set-webln' connects a WebLN extension (or says there is none, or that it declined);
    '#us-tg-unlink' (confirmed) unlinks Telegram and the card then says "Not linked"; the mail Folders dialog's
    '#usf-save' PUTs the chosen folders for that account and closes (a refusal is said and the button comes back);
    '#us-instance-go' switches the app to a typed PosterChan instance and refuses a site that is not one;
    '#us-ntor-new' asks the desktop shell's Tor for a new circuit; '#al-play' plays the PosterChan Alert file.
  * Android: '#nip55-on' hands the session's key to the Signer plugin and the card turns to '#nip55-off', which
    takes it back; '#bunker-copy' copies the bunker:// link shown; '#us-tor-start' asks Orbot to start and
    '#us-tor-open' opens it.
The Telegram check runs with the desktop shell's Tor ON, and that is what found the bug it guards: the Tor switch
was painted on by code (checked != defaultChecked), renderUserSettings took that for somebody's unsaved edit and
refused every redraw, so Unlink Telegram unlinked and the card went on saying "Linked".
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_settings_account_full_app import open_tab_holding

ME_HEX = bip340.pubkey_from_seckey(b"\x01" * 32).hex()          # desktop.login's key
ME = nostr_service.npub_of(ME_HEX)

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Plain layout; every toast and every copy recorded (a toast lives 3.2 s, a check may look later).
FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__toasts=[];window.__clip=[];
new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{if(n.classList&&n.classList.contains('toast'))__toasts.push(n.textContent);}))).observe(document,{childList:true,subtree:true});
const __exec=document.execCommand.bind(document);
document.execCommand=function(c,...r){ if(c==='copy'){ const a=document.activeElement; __clip.push(a&&'value' in a?a.value:String(getSelection())); return true; } return __exec(c,...r); };
try{ navigator.clipboard.writeText=async t=>{ __clip.push(String(t)); }; }catch(_){}
// The page a button sends us to is a real navigation: note where we ARRIVED, before the app tidies the URL.
if(/[?&]gconsent=1/.test(location.search))sessionStorage.setItem('__arrived',location.href);
'''

# The server, at the network boundary: what each button sends is recorded with its body.
SERVER = r'''
window.__calls=[];
window.__srv=Object.assign({settings:{},providers:{google:true},delAcct:{ok:true},google:{detail:'Google sign-in is not set up here'},
  folderPut:{ok:true}},window.__srv||{});
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const s=String(url),m=String(opts.method||'GET').toUpperCase(),J=(v,st=200)=>new Response(JSON.stringify(v),{status:st,headers:{'Content-Type':'application/json'}});
  const rec=()=>{let b=opts.body;try{b=JSON.parse(b);}catch(_){}__calls.push({url:s,method:m,body:b??null});};
  if(s.includes('/client/config')){
    if(s.includes('relay.example'))return new Response('not here',{status:404});
    const r=await prev.apply(this,arguments);const d=await r.json();return J({...d,admin_npubs:[%(me)r]});}
  if(s.includes('/api/auth/settings')&&m==='GET')return J(__srv.settings);
  if(s.includes('/api/auth/providers'))return J(__srv.providers);
  if(s.includes('/client/delete-account')){rec();return J(__srv.delAcct);}
  if(s.includes('/api/auth/google/link/start')){rec();return J(__srv.google);}
  if(s.includes('/api/telegram/unlink')){rec();__srv.settings={...__srv.settings,telegram_chat_id:null};return J({ok:true});}
  if(s.includes('/api/mail/folder-map')){rec();return J(m==='PUT'?__srv.folderPut:
    {folders:['INBOX','Sent','Sent Messages','INBOX.Sent','Trash'],mapping:{},detected:{sent:'Sent',trash:'Trash'}});}
  return prev.apply(this,arguments);};}
''' % {"me": ME}

TOASTS = "__toasts.join(' | ')"


def calls_to(path):
    return f"__calls.filter(c=>c.url.includes({json.dumps(path)}))"


async def confirm(b, text):
    """Press OK on the confirm dialog that says `text` (the page behind it may say the same words)."""
    pick = f"[...document.querySelectorAll('.uiconfirm')].find(d=>d.textContent.includes({json.dumps(text)})&&d.querySelector('[data-uc=\"1\"]'))"
    await b.until(f"!!{pick}")
    await b.js(f"{pick}.querySelector('[data-uc=\"1\"]').click(); 1")


async def signed_auth(b, body, content):
    """The base64 NIP-98-style event a button sent must be a valid signature by ME over `content`."""
    ev = json.loads(base64.b64decode(body['auth']))
    assert ev['kind'] == 27235 and ev['content'] == content and ev['pubkey'] == ME_HEX, ev
    assert ['p', ME_HEX] in ev['tags'], ev
    assert await b.js(f"NostrTools.verifyEvent({json.dumps(ev)})"), 'the auth event is not validly signed'


@CHROME
def test_account_card_admin_google_link_and_delete_account():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('settings'); 1")
        # Admin panel: shown to an admin, and it opens the admin view.
        await b.until("!!document.querySelector('#set-admin')")
        await b.js("document.querySelector('#set-admin').click(); 1")
        await b.until("document.querySelector('#view-title') && /admin/i.test(document.querySelector('#view-title').textContent)")

        # Google link: shown because the node runs Google sign-in; a refusal is said.
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#set-google-link') && !document.querySelector('#set-google-link').classList.contains('hidden')")
        await b.js("document.querySelector('#set-google-link').click(); 1")
        await confirm(b, 'Link Google to this account?')
        await b.until(f"{calls_to('/api/auth/google/link/start')}.length===1 && /Google sign-in is not set up here/.test({TOASTS})")
        body = await b.js(f"{calls_to('/api/auth/google/link/start')}[0].body")
        nsec = await b.js("NostrTools.nip19.nsecEncode(new Uint8Array(32).fill(1))")
        assert body['pubkey'] == ME_HEX and body['nsec'] == nsec, body
        await signed_auth(b, body, 'google-link')
        # ...and with an auth_url the page goes there.
        await b.js("__srv.google={auth_url:location.origin+'/index.html?gconsent=1'}; 1")
        await b.js("document.querySelector('#set-google-link').click(); 1")
        await confirm(b, 'Link Google to this account?')
        await b.until("/[?&]gconsent=1/.test(sessionStorage.getItem('__arrived')||'')")
        await b.until("!!document.body && !!window.__PC && !!__PC.me()")

        # Delete my account: a refusal is said and you stay signed in.
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#set-del-account')")
        await b.js("__srv.delAcct={ok:false,error:'not on this server'}; document.querySelector('#set-del-account').click(); 1")
        await confirm(b, 'Permanently delete your account')
        await b.until(f"/delete failed: not on this server/.test({TOASTS})")
        body = await b.js(f"{calls_to('/client/delete-account')}[0].body")
        assert body['pubkey'] == ME_HEX, body
        await signed_auth(b, body, 'delete-account')
        await asyncio.sleep(1.2)
        assert await b.js("!!__PC.me() && !!localStorage.getItem('pc_nostr_session')"), 'a refused delete signed you out'
        # ...and an ok one says so, clears the session and reloads to the signed-out screen.
        await b.js("__srv.delAcct={ok:true}; document.querySelector('#set-del-account').click(); 1")
        await confirm(b, 'Permanently delete your account')
        await b.until(f"/account deleted/.test({TOASTS}) && !localStorage.getItem('pc_nostr_session')")
        await b.until("!!document.body && document.body.classList.contains('guest')")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + SERVER))


AUDIO_PROBE = r"""window.__played=[]; HTMLMediaElement.prototype.play=function(){ __played.push(String(this.src||'')); return Promise.resolve(); };"""

# The desktop shell's own Tor (window.pcShell.tor), bootstrapped and running.
NATIVE_TOR = r'''
window.__tor={circuits:0,fail:false};
window.pcShell.tor={status:async()=>({enabled:true,running:true,bootstrapped:true,progress:100,country:'',countries:[['de','Germany']]}),
  set:async o=>({enabled:true,running:true,bootstrapped:true}),onStatus:()=>{},
  newCircuit:async()=>{ if(__tor.fail) throw new Error('control port closed'); __tor.circuits++; return {ok:true}; }};
'''

USER = r'''
// The desktop bundle (its __PC_SET_INSTANCE__ hands the choice to the shell): record what the shell is told.
window.__setInstance=[];window.pcShell.setInstance=async u=>{__setInstance.push(u);return true;};
window.__srv={settings:{telegram_chat_id:4242,mail_accounts:[{email:'me@mail.example',imap_server:'imap.mail.example',imap_port:993,smtp_server:'smtp.mail.example',smtp_port:587}]}};
'''


@CHROME
def test_user_settings_webln_telegram_folders_instance_tor_and_alert():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('settings'); 1")

        # Connect Alby / WebLN: none, then one that declines, then one that connects.
        await b.until("!!document.querySelector('#set-webln')")
        await open_tab_holding(b, '#set-webln')
        st = "document.querySelector('#set-nwc-status').textContent"
        await b.js("delete window.webln; document.querySelector('#set-webln').click(); 1")
        await b.until(f"/No WebLN extension found/.test({st})")
        await b.js("window.webln={enable:async()=>{throw new Error('User rejected')}}; document.querySelector('#set-webln').click(); 1")
        await b.until(f"/Extension declined: User rejected/.test({st})")
        await b.js("window.__weblnOn=0; window.webln={enable:async()=>{__weblnOn++;}}; document.querySelector('#set-webln').click(); 1")
        await b.until(f"/Extension connected/.test({st}) && __weblnOn===1 && /wallet extension connected/.test({TOASTS})")

        # Unlink Telegram: confirmed, the server is told, and the card says it is no longer linked.
        await open_tab_holding(b, '#us-tg-unlink')
        assert await b.js("/Linked \\(chat 4242\\)/.test(document.querySelector('#us-tg-status').textContent)")
        await b.js("document.querySelector('#us-tg-unlink').click(); 1")
        await confirm(b, 'Unlink Telegram?')
        await b.until(f"{calls_to('/api/telegram/unlink')}.length===1 && {calls_to('/api/telegram/unlink')}[0].method==='POST' && /unlinked/.test({TOASTS})")
        await b.until("!!document.querySelector('#us-tg-status') && /Not linked/.test(document.querySelector('#us-tg-status').textContent) && !document.querySelector('#us-tg-unlink')")

        # Mail → Folders → Save folders: what was picked is PUT for THIS account, and the dialog closes.
        await b.until("!!document.querySelector('.us-mail-folders')")
        await open_tab_holding(b, '.us-mail-folders')
        await b.js("document.querySelector('.us-mail-folders').click(); 1")
        await b.until("!!document.querySelector('#usf-save') && document.querySelectorAll('.usf-sel').length===5")
        await b.js("(()=>{const s=document.querySelector('.usf-sel[data-role=sent]');s.value='Sent Messages';s.dispatchEvent(new Event('change',{bubbles:true}));})(); 1")
        await b.js("__srv.folderPut={detail:'IMAP said no'}; document.querySelector('#usf-save').click(); 1")
        await b.until(f"/could not save: IMAP said no/.test({TOASTS})")
        await b.until("!!document.querySelector('#usf-save') && !document.querySelector('#usf-save').disabled && document.querySelector('#usf-save').textContent==='Save folders'")
        await b.js("__srv.folderPut={ok:true}; document.querySelector('#usf-save').click(); 1")
        await b.until(f"/folders saved/.test({TOASTS}) && !document.querySelector('#usf-save')")
        puts = await b.js(f"{calls_to('/api/mail/folder-map')}.filter(c=>c.method==='PUT').map(c=>c.body)")
        assert puts[-1] == {'account': 'me@mail.example', 'mapping': {'sent': 'Sent Messages'}}, puts

        # Instance → Connect: a site that is not a PosterChan server is refused; a real one is switched to.
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#us-instance-go')")
        await open_tab_holding(b, '#us-instance-go')
        await b.js("document.querySelector('#us-instance-inp').value='relay.example'; document.querySelector('#us-instance-go').click(); 1")
        await b.until(f"/relay\\.example is not a PosterChan server/.test({TOASTS})")
        assert await b.js("__setInstance") == []
        await b.js("document.querySelector('#us-instance-inp').value='  other.example/ '; document.querySelector('#us-instance-go').click(); 1")
        await b.until("__setInstance.length===1")
        assert await b.js("__setInstance") == ['https://other.example']
        assert 'https://other.example' in await b.js("JSON.parse(localStorage.getItem('pc_instances')||'[]')")

        # Tor (desktop shell): New circuit asks the shell for one and says so; a failure is said too.
        await open_tab_holding(b, '#us-ntor-new')
        await b.until("!document.querySelector('#us-ntor-new').disabled && /Connected through Tor/.test(document.querySelector('#us-ntor-state').textContent)")
        await b.js("document.querySelector('#us-ntor-new').click(); 1")
        await b.until(f"__tor.circuits===1 && /new Tor circuit/.test({TOASTS}) && !document.querySelector('#us-ntor-new').disabled")
        await b.js("__tor.fail=true; document.querySelector('#us-ntor-new').click(); 1")
        await b.until(f"/could not get a new circuit/.test({TOASTS})")
        assert await b.js("__tor.circuits") == 1

        # Sounds → ▶ Play the PosterChan Alert: the alert file plays, from the start, every press.
        await b.js("document.querySelector('.us-tab[data-tab=\"ringtone\"]').click(); 1")
        await b.until("!!document.querySelector('[data-pane=\"ringtone\"].active #al-play')")
        await b.js("document.querySelector('#al-play').click(); document.querySelector('#al-play').click(); 1")
        await b.until("__played.length===2")
        assert all(p.endswith('/static/sounds/posterchan-alert.ogg') for p in await b.js("__played")), await b.js("__played")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + USER + SERVER + NATIVE_TOR + AUDIO_PROBE))


# The Android app: a Signer plugin that keeps the key it is handed, and Orbot.
ANDROID = r'''
window.__nip55={exposed:false,calls:[]};window.__orbot=[];
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{
  Signer:{status:async()=>({have:__nip55.exposed,exposed:__nip55.exposed}),
    enable:async o=>{__nip55.calls.push(['enable',o.sec]);__nip55.exposed=true;return {pubkey:'x'};},
    disable:async()=>{__nip55.calls.push(['disable']);__nip55.exposed=false;return {};}},
  Orbot:{isInstalled:async()=>({installed:true}),
    start:async()=>{__orbot.push('start');return {requested:true};},
    openApp:async()=>{__orbot.push('open');return {};}}}};
'''


@CHROME
def test_android_signer_for_other_apps_bunker_link_and_orbot():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){} __PC.switchView('signer'); 1")

        # Sign for other apps: turn on hands over THIS session's key; the card then offers turn off.
        await b.until("!!document.querySelector('#nip55-on')")
        await b.js("document.querySelector('#nip55-on').click(); 1")
        await b.until(f"!!document.querySelector('#nip55-off') && /can now sign for other apps/.test({TOASTS})")
        sec = await b.js("JSON.parse(localStorage.getItem('pc_nostr_session')).sk")
        assert sec and await b.js("__nip55.calls") == [['enable', sec]]
        await b.js("document.querySelector('#nip55-off').click(); 1")
        await b.until(f"!!document.querySelector('#nip55-on') && /will no longer sign for other apps/.test({TOASTS})")
        assert await b.js("__nip55.calls.map(c=>c[0])") == ['enable', 'disable']

        # Connect an app with a link: Copy copies exactly the bunker:// link the sheet shows.
        await b.until("!!document.querySelector('#signer-bunker')")
        await b.js("document.querySelector('#signer-bunker').click(); 1")
        await b.until("!!document.querySelector('#bunker-copy') && /^bunker:\\/\\//.test(document.querySelector('#bunker-uri').value)")
        uri = await b.js("document.querySelector('#bunker-uri').value")
        assert uri.startswith(f"bunker://{ME_HEX}?relay="), uri
        await b.js("document.querySelector('#bunker-copy').click(); 1")
        await b.until(f"__clip.includes({json.dumps(uri)}) && /link copied/.test({TOASTS})")
        await b.js("document.querySelector('#bunker-done').click(); 1")
        await b.until("!document.querySelector('#bunker-copy')")

        # Settings → Tor (Orbot): Start asks Orbot to start and says so; Open opens it.
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#us-tor-start')")
        await open_tab_holding(b, '#us-tor-start')
        await b.until("/Orbot is installed/.test(document.querySelector('#us-tor-state').textContent)")
        await b.js("document.querySelector('#us-tor-start').click(); 1")
        await b.until(f"__orbot.includes('start') && /asked Orbot to start/.test({TOASTS})")
        await b.js("document.querySelector('#us-tor-open').click(); 1")
        await b.until("__orbot.includes('open')")
        assert await b.js("__orbot") == ['start', 'open']
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + SERVER + ANDROID))
