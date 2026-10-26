"""App-wide buttons, pressed in the SHIPPED bundle:

  * a screen nothing can show offers "Go home", which goes home;
  * Messages → New message → a recipient and a message → Send sends a private message (a NIP-17 gift wrap) and
    opens the conversation;
  * the account switcher's "Add an account" signs this one out to the sign-in screen (the account stays listed);
  * sign-up: Generate shows the captcha, and its ↻ fetches a new one.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sent=[];window.__captchas=0;
const __as=WebSocket.prototype.send;
WebSocket.prototype.send=function(r){try{const m=JSON.parse(r);if(m[0]==='EVENT')__sent.push(m[1]);}catch(_){}return __as.call(this,r)};
const __af=window.fetch;
window.fetch=(u,o)=>{if(String(u).includes('/client/captcha')){__captchas++;
  return Promise.resolve(new Response(JSON.stringify({token:'t'+__captchas,image:'data:image/gif;base64,R0lGODlhAQABAAAAACw='}),{status:200,headers:{'Content-Type':'application/json'}}));}
  return __af(u,o);};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_go_home_and_send_a_new_message():
    async def check(b):
        await desktop.login(b)
        await b.js("document.querySelector('#feed').innerHTML=''; __PC.switchView('no-such-screen'); 1")
        await b.until("!!document.querySelector('#view-home')")
        await b.js("document.querySelector('#view-home').click(); 1")
        await b.until("!document.querySelector('#view-home') && !!document.querySelector('#tl-cmp-ta')")

        friend = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(6))")
        await b.js("window.__publishOK=true; __PC.switchView('messages'); 1")
        await b.until("!!document.querySelector('#dm-new')")
        await b.js("document.querySelector('#dm-new').click(); 1")
        await b.until("!!document.querySelector('#dm-go')")
        await b.js(f"(()=>{{const t=document.querySelector('#dm-to');t.value=NostrTools.nip19.npubEncode({json.dumps(friend)});"
                   "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#dm-body').value='hi from the test';"
                   "document.querySelector('#dm-go').click();})(); 1")
        await b.until("__sent.some(e=>e.kind===1059) && !document.querySelector('#dm-go')")
        await b.until("!!document.querySelector('#dm-in')")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_add_an_account_signs_out_to_the_sign_in_screen():
    async def check(b):
        await desktop.login(b)
        me = await b.js("__PC.me().pubkey")
        await b.js("__PC.accountMenu(); 1")
        await b.until("!!document.querySelector('#acct-add')")
        await b.js("document.querySelector('#acct-add').click(); 1")
        # Signing out reloads the page, and the old page could satisfy a one-shot wait just before it went: the next
        # evaluate then landed in a half-loaded document ("ReferenceError: __PC", flaky under load in 3 deploys). So
        # "signed out on the sign-in screen" is the wait itself, and it must still hold once the page has settled.
        out = ("!!window.__PC && !!document.body && (document.body.classList.contains('guest') || "
               "!!document.querySelector('#btn-nip07:not([hidden])')) && !(__PC.me&&__PC.me()&&__PC.me().pubkey)")

        async def signed_out():
            # b.until() raises on the CDP error an evaluate gets while the page is between documents
            # ("Inspected target navigated or closed") -- which is exactly when this wait runs. Poll through it.
            for _ in range(150):
                try:
                    if await b.js(out):
                        return
                except AssertionError:
                    pass
                await asyncio.sleep(.1)
            raise AssertionError(("not signed out on the sign-in screen after Add an account",
                                  await b.js("({body: document.body ? document.body.innerText.slice(-600) : ''})")))
        await signed_out()
        await asyncio.sleep(1.5)
        await signed_out()
        listed = await b.js("JSON.stringify(localStorage)")
        assert me in listed, "the account was forgotten instead of kept in the switcher"
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_sign_up_captcha_refreshes():
    async def check(b):
        # Booted, as a person must be: every Sign up button is drawn by startApp(), and before it the sign-in screen's
        # buttons are not wired yet -- opened that early (only a test can), Generate keypair did nothing and the boot's
        # end then closed the form (flaky under load in 3 deploys).
        await b.until("!!window.__PC && !!__PC.showAuth && !!window.__PC_BOOTED")
        await b.js("__PC.showAuth('signup'); 1")
        await b.until("!!document.querySelector('#btn-gen-key')")
        await b.js("document.querySelector('#btn-gen-key').click(); 1")
        await b.until("!!document.querySelector('#signup-captcha-refresh') && __captchas===1")
        await b.js("document.querySelector('#signup-captcha-refresh').click(); 1")
        await b.until("__captchas===2")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))

