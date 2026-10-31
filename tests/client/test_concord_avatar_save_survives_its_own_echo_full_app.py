"""SAVING A COMMUNITY AVATAR SAYS "SAVED" -- ITS OWN ECHO IS NOT A MEMBERSHIP CHANGE.

"Every time I update concord community avatar it works but gives me error: Not saved: Concord membership changed during
profile update" (2026-10-30). The save publishes the community's new profile; the open community's live control
subscription receives that very envelope and refreshes, re-storing the room's bundle with what the profile says -- its
relay list (this node's backups get listed) and public-channel history. The save's guard compared the WHOLE stored
bundle, so that cosmetic rewrite read as "membership changed" after the profile had already gone out.

Shipped bundle, phone size, the real settings dialog and a real PosterCord community; only relays and Blossom are
fixtures. While the publish is in flight the stored bundle is rewritten exactly as the refresh does it. Then:
  1. a cosmetic rewrite (relays, public-channel history, name) -> the save reports success, no "Not saved";
  2. a REAL membership change (the community root's epoch moves) -> the save still refuses, as it must.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_icon_upload_says_so_full_app import NO_OS, ROOM, PICK, open_settings, wait


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ECHO = r'''((mode)=>{const p=__PC,key='pc.concord.rooms.v1.'+p.me().pubkey;
  window.__store=new Map();
  p.uploadBlob=async blob=>{const bytes=new Uint8Array(await blob.arrayBuffer()),url='https://fixture.invalid/blossom/'+'e'.repeat(64);
    __store.set(url,bytes);__uploads.push({size:bytes.length,state:'released'});return url;};
  const f=window.fetch;window.fetch=async(u,o)=>{const k=String(u&&u.url||u);if(__store.has(k))return new Response(__store.get(k),{status:200});return f(u,o);};
  p.relayPublishRoom=async(relays,ev)=>{__pub.push(ev);
    /* what the live refresh does to the stored room when this envelope comes back */
    const rooms=JSON.parse(localStorage.getItem(key)||'[]'),b=rooms[0].cord.bundle;
    if(mode==='cosmetic'){b.relays=[...(b.relays||[]),'wss://backup.invalid'];b.public_channel_history=['ab'.repeat(32)];b.name='Icon room';}
    else {b.root_epoch=String(BigInt(b.root_epoch||0)+1n);}
    localStorage.setItem(key,JSON.stringify(rooms));
    return {ok:true,relays};};
  return true;})'''

TOASTS = "[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')"


async def save_avatar(b, mode):
    await open_settings(b)
    await b.js(ECHO + "(%r)" % mode)
    await b.js(PICK)
    await wait(b, "(document.querySelector('#cc-settings-icon-status')||{}).textContent.includes('uploaded')", 15)
    await b.js("window.__toasts=[];const T=__PC.toast;__PC.toast=(m,...a)=>{__toasts.push(String(m));return T(m,...a);};"
               "new MutationObserver(()=>document.querySelectorAll('.toast').forEach(t=>{if(!__toasts.includes(t.textContent))__toasts.push(t.textContent)})).observe(document.body,{childList:true,subtree:true}); 1")
    await b.js("document.querySelector('#cc-settings-save').click()")
    await wait(b, "__pub.some(e=>e.kind===1059)", 15)
    await asyncio.sleep(2)
    return await b.js("__toasts.join(' | ')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_saves_own_echo_is_not_a_membership_change():
    got = {}

    async def check(b):
        got['toasts'] = await save_avatar(b, 'cosmetic')
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    assert 'membership changed' not in got['toasts'] and 'Not saved' not in got['toasts'], got['toasts']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_real_membership_change_during_the_save_is_still_refused():
    got = {}

    async def check(b):
        got['toasts'] = await save_avatar(b, 'rekey')
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    assert 'membership changed' in got['toasts'], got['toasts']
