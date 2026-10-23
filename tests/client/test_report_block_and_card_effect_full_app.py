"""A post's ⋯ menu, the two buttons the dialog tests left unpressed (2026-10-22: every button gets a test).

  * Report → (sent) → '#rep-block' "Block on this relay": offered to an ADMIN after a public report, and only to an
    admin. It ASKS first; Cancel sends nothing; OK sends exactly one POST /client/block whose `target` is the
    reported author and whose `auth` is a kind-27235 event signed by the admin carrying ['action','block'] and
    ['p', <author>]; the toast says "blocked on relay".
  * Screenshot → '#ss-fx' "Effect": closes the card dialog and opens the Effects studio ON THE CARD -- the AI view
    loads the card's own uploaded link (through /client/proxy-image) and the studio shows it as the thumbnail.

The report sheet's "Ban from this room" button (menus.js, shown when `opts.ban` is a function) is not tested,
and deliberately not named here so the coverage scan keeps counting it as untested: no caller has ever passed `opts.ban` to
openReport (`git log -S'opts.ban'` finds only 769b06747, which added the button and no caller), so no screen
can show it.
Real login, DOM, cards.js and menus.js; the relay socket and HTTP are fixtures that record what was sent.
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_post_menu_dialogs_full_app import FIXTURE, SETUP, SHOT, menu


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ME = nostr_service.npub_of(bip340.pubkey_from_seckey(b"\x01" * 32).hex())     # desktop.login's key

# This node's admins are its config's `admin_npubs` (app.js IS_ADMIN), so the logged-in account is made one.
ADMIN = r'''
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  if(String(url).includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();
    return new Response(JSON.stringify({...d,admin_npubs:[%(me)r]}),{status:200,headers:{'Content-Type':'application/json'}});}
  return prev.apply(this,arguments);};}
''' % {"me": ME}

BLOCK = r'''
window.__blocks=[];
const __bf=window.fetch;
window.fetch=(u,o)=>{if(/\/client\/block(\?|$)/.test(String(u))){__blocks.push(JSON.parse(o.body));
  return Promise.resolve(new Response(JSON.stringify({ok:true}),{status:200,headers:{'Content-Type':'application/json'}}));}
  return __bf(u,o);};
'''

async def report_post(b):
    await menu(b, "report")
    await b.until("!!document.querySelector('input[name=rep-type][value=spam]')")
    await b.js("(()=>{const r=document.querySelector('input[name=rep-type][value=spam]');r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));"
               "document.querySelector('#rep-submit').click();})(); 1")
    await b.until("!!document.querySelector('.rep-done')")


def toast_has(text):
    return "[...document.querySelectorAll('#toast-root .toast')].some(t=>t.textContent===" + json.dumps(text) + ")"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_admin_blocks_the_reported_author_on_the_relay_after_confirming():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        await report_post(b)
        await b.until("!!document.querySelector('#rep-block')")
        assert "Block on this relay" in await b.js("document.querySelector('#rep-block').textContent")
        # It asks first, and Cancel sends nothing.
        await b.js("document.querySelector('#rep-block').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        assert "Block this npub on the relay" in await b.js("document.querySelector('.uiconfirm-msg').textContent")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"0\"]').click(); 1")
        await b.until("!document.querySelector('.uiconfirm-btns')")
        await asyncio.sleep(.3)
        assert await b.js("__blocks.length") == 0, "Cancel still blocked the author"
        # OK: exactly one signed block naming the reported author.
        await b.js("document.querySelector('#rep-block').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until("__blocks.length===1")
        await b.until(toast_has("blocked on relay"))
        sent = await b.js("__blocks[0]")
        pk = await b.js("__post.pk")
        assert sent["target"] == pk, sent
        auth = json.loads(base64.b64decode(sent["auth"]))
        assert auth["kind"] == 27235 and auth["pubkey"] == await b.js("__PC.me().pubkey") and auth["sig"], auth
        assert ["action", "block"] in auth["tags"] and ["p", pk] in auth["tags"], auth["tags"]
        assert await b.js("NostrTools.verifyEvent(" + json.dumps(auth) + ")"), "the block proof is not a valid signature"
        await asyncio.sleep(.3)
        assert await b.js("__blocks.length") == 1
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + BLOCK + ADMIN))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_non_admin_is_not_offered_block_on_this_relay():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        await report_post(b)
        await b.until("!!document.querySelector('#rep-close')")
        assert not await b.js("!!document.querySelector('#rep-block')"), "a non-admin was offered a relay block"
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + BLOCK))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_screenshot_card_effect_opens_the_effects_studio_on_the_card():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        await menu(b, "screenshot")
        await b.until("!!document.querySelector('#ss-fx')")
        link = await b.js("document.querySelector('.ss-acts').closest('.modal, .modal-card, [role=dialog], body').querySelector('img').getAttribute('src')")
        await b.js("window.__reqBefore=__requests.length; document.querySelector('#ss-fx').click(); 1")
        await b.until("!document.querySelector('#ss-fx')")
        await b.until("!!document.querySelector('#fxs-go')")
        want = "/client/proxy-image?url=" + await b.js("encodeURIComponent(" + json.dumps(link) + ")")
        assert await b.js("__requests.slice(__reqBefore).some(r=>r[0].includes(" + json.dumps(want) + "))"), \
            ("the studio did not load the card's own link", link, await b.js("__requests.slice(__reqBefore).map(r=>r[0])"))
        assert await b.js("!!document.querySelector('.fxs .fxs-thumb')"), "the studio does not show the card"
        assert await b.js("__PC.view?__PC.view():document.body.dataset.view||''") in ("ai", ""), "not in the AI view"
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + SHOT))
