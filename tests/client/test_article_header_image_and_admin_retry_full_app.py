"""Two Discover buttons no test pressed in the shipped bundle (2026-10-22: every button gets a test).

  * Write article → '#ae-img-up' "Upload header image": opens the header-image picker (not the body's), uploads the
    picked file, fills the Header image field with the uploaded link, and Publish carries it as the article's
    ['image', <link>] tag.
  * Admin → '#admin-session-retry' "Retry admin sign-in": when the instance refused the admin sign-in, the page says
    so with the server's reason and a Retry; pressing it once the server answers signs in again (a fresh
    /api/auth/nostr-login) and opens the admin panel (the /admin frame) in place of the error.
"""
import asyncio
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_articles_and_torrents_buttons_full_app import FIXTURE, SENT


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_upload_header_image_fills_the_field_and_publishes_it():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('articles'); 1")
        await b.until("!!document.querySelector('#art-new')")
        await b.js("document.querySelector('#art-new').click(); 1")
        await b.until("!!document.querySelector('#ae-img-up')")
        await b.js("(()=>{document.querySelector('#ae-title').value='With a header';document.querySelector('#ae-body').value='Body text.';})(); 1")
        assert await b.js("document.querySelector('#ae-img').value") == ''
        await b.js("window.__picked=''; document.querySelector('#ae-img-up').click(); 1")
        await b.until("window.__picked==='ae-img-file'")
        await b.js("window.__upBefore=__requests.filter(r=>/upload/.test(r[0])).length;"
                   "(()=>{const i=document.querySelector('#ae-img-file');const dt=new DataTransfer();"
                   "dt.items.add(new File([new Uint8Array([137,80,78,71])],'header.png',{type:'image/png'}));"
                   "i.files=dt.files;i.dispatchEvent(new Event('change'));})(); 1")
        await b.until("document.querySelector('#ae-status').textContent==='image uploaded'")
        url = await b.js("document.querySelector('#ae-img').value")
        assert url.startswith('http') and url.endswith('/fixture.png'), url
        assert await b.js("__requests.filter(r=>/upload/.test(r[0])).length>__upBefore"), "nothing was uploaded"
        # The body was not touched: the header image is not an inline image.
        assert await b.js("document.querySelector('#ae-body').value") == 'Body text.'
        await b.js("document.querySelector('#ae-pub').click(); 1")
        await b.until(f"{SENT % 30023}.length===1")
        art = await b.js(f"{SENT % 30023}[0]")
        assert ["image", url] in art["tags"], art["tags"]
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


ME = nostr_service.npub_of(bip340.pubkey_from_seckey(b"\x01" * 32).hex())     # desktop.login's key

# This account is one of the node's admins (config admin_npubs), and its sign-in is refused until told otherwise.
ADMIN = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__loginDown=true;window.__logins=0;
{const prev=window.fetch;window.fetch=async function(url,opts={}){const u=String(url);
  const J=(o,s)=>new Response(JSON.stringify(o),{status:s,headers:{'Content-Type':'application/json'}});
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();return J({...d,admin_npubs:[%(me)r]},200);}
  if(u.includes('/api/auth/nostr-login')){__logins++;if(__loginDown)return J({detail:'admin sign-in is down for maintenance'},503);}
  return prev.apply(this,arguments);};}
''' % {"me": ME}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_retry_admin_sign_in_signs_in_again_and_opens_the_panel():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('admin'); 1")
        await b.until("!!document.querySelector('#admin-session-retry')")
        text = await b.js("document.querySelector('#feed').innerText")
        assert 'Admin sign-in failed' in text and 'admin sign-in is down for maintenance' in text, text
        assert not await b.js("!!document.querySelector('#admin-host iframe') && document.querySelector('#admin-host').style.display==='block'")
        # The server is back: Retry signs in again and the panel replaces the error.
        await b.js("window.__loginDown=false; window.__before=__logins; document.querySelector('#admin-session-retry').click(); 1")
        await b.until("__logins>__before")
        await b.until("document.querySelector('#admin-host')?.style.display==='block'")
        src = await b.js("document.querySelector('#admin-host iframe').getAttribute('src')||''")
        assert '/admin' in src, src
        assert await b.js("document.querySelector('#feed').style.display") == 'none'
        assert not await b.js("!!document.querySelector('#admin-session-retry')")
    asyncio.run(desktop.with_browser('online', '', check, ADMIN))
