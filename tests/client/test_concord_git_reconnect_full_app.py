"""A repository disconnected in Community settings can be connected again.

Reported 2026-10-22: "I removed posterchan repo from community settings, then tried to add the posterchan repo back
but I can't select it." Real minted community, real reader writing the channel editions (Armada's attach/detach
interval shape), real signer and DOM; the relay is the shared in-page fixture (tests/client/concord_fixture_relay.py)
holding a signed kind-30617 announcement. Settings → Connect → pick → channel, Disconnect, then Connect again: the
repository must be selectable, the channel offered, and the second interval published.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM, CONTROLS
from tests.client.test_concord_channel_controls_full_app import click as _click


async def click(b, selector):
    """Scrolled into view first, as a person would, then pressed with a real pointer where it is drawn."""
    await b.js("document.querySelector(%s).scrollIntoView({block:'center',behavior:'instant'})" % json.dumps(selector))
    await b.js("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
    await _click(b, selector)


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ANNOUNCE = r'''(async()=>{
  const ev=await __PC.signTemplate({kind:30617,created_at:Math.floor(Date.now()/1000)-86400,content:'',
    tags:[['d','posterchanai'],['name','posterchanai'],['description','PosterChan'],['relays','wss://git.fixture.invalid']]});
  __relaySeed([ev]);window.__coord='30617:'+ev.pubkey+':posterchanai';return window.__coord;
})()'''

ATTACHED = "(()=>{const ch=PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='general');" \
           "return PCConcordGit.attachmentsOf(ch).filter(a=>a.address.coordinate===__coord).map(a=>a.detachedAt===undefined?'on':'off')})()"


async def open_settings(b):
    await b.js("document.querySelector('#cc-settings-dialog')&&document.querySelector('#cc-settings-dialog').classList.add('hidden')")
    await b.until("!!document.querySelector('#cc-edit-icon')")
    await click(b, '#cc-edit-icon')
    await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden') && !!document.querySelector('#cc-settings-dialog .ccg-settings')")
    # Opening settings puts the cursor in Description a moment later, which scrolls the card back to its top: wait for
    # it, as anyone does, before scrolling to a control further down.
    await b.until("document.activeElement && document.activeElement.id==='cc-description-value'")


async def connect(b):
    await open_settings(b)
    await click(b, '#cc-settings-dialog [data-ccg-connect]')
    await b.until("!!document.querySelector('#ccg-pick-q')")
    await b.js("(()=>{const q=document.querySelector('#ccg-pick-q');q.value='posterchan';q.dispatchEvent(new Event('input'));})()")
    await b.until("!!document.querySelector('#ccg-pick-list [data-ccg-pick]')")
    row = await b.js("(()=>{const r=document.querySelector('#ccg-pick-list [data-ccg-pick=\"'+__coord+'\"]');return r?{disabled:r.disabled,text:r.innerText}:null})()")
    assert row and not row['disabled'], ('the repository cannot be selected', row)
    await b.js("document.querySelector('#ccg-pick-list [data-ccg-pick=\"'+__coord+'\"]').click()")
    await b.until("!!document.querySelector('[data-ccg-ch]')")
    general = await b.js("(()=>{const ch=PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='general');"
                         "const r=document.querySelector('[data-ccg-ch=\"'+ch.id+'\"]');return r?{disabled:r.disabled,text:r.innerText}:null})()")
    assert general and not general['disabled'], ('#general is not offered', general)
    await b.js("(()=>{const ch=PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='general');"
               "document.querySelector('[data-ccg-ch=\"'+ch.id+'\"]').click()})()")


async def setup(b, width=1280):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js("__PC.switchMessagesTab('concord')")
    await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord && !!window.PCConcordGit')
    await b.js(INSTALL)
    await b.js("(()=>{window.__toasts=[];const t=__PC.toast;__PC.toast=m=>{__toasts.push(String(m));try{t(m)}catch(_){}};})()")
    minted = await b.js(MINT.replace('NAME', json.dumps('Git reconnect')))
    await b.js(ANNOUNCE)
    # The git module reads through Concord's relay door; the same store answers it.
    await b.js("(()=>{const door={queryFrom:async(_u,f)=>__PC.relayQueryFrom(_u,f),publishTo:async(_u,ev)=>__PC.relayPublishRoom(_u,ev),"
               "subscribeFrom:(_u,f,o)=>Relay.subscribeFrom(_u,f,o)};"
               "Object.defineProperty(window,'PCConcordRelay',{configurable:true,get(){return door;}});})()")
    await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_disconnected_repository_can_be_connected_again():
    async def check(b):
        await setup(b)
        await connect(b)
        await b.until(ATTACHED + ".join()==='on'")

        await open_settings(b)
        await b.until("!!document.querySelector('#cc-settings-dialog [data-ccg-detach]')")
        await click(b, '#cc-settings-dialog [data-ccg-detach]')
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click()")
        await b.until(ATTACHED + ".join()==='off'")

        await connect(b)
        await b.until(ATTACHED + ".join()==='off,on'")
        assert not [t for t in await b.js('__toasts') if 'not connected' in t], await b.js('__toasts')
        assert not await b.js('__errors'), await b.js('__errors')

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_a_new_repository_channel_with_no_name_appears_named_after_the_repository(width):
    """Reported 2026-10-22: "connecting git worked, I forgot to enter a name in the previous screen, but I don't see any
    channel added". + New channel → Git repository, name left blank (it is named after the repository), pick it: the
    channel must be in the list, carry the repository, and be the one on screen."""
    async def check(b):
        await setup(b, width)
        await b.until("!!document.querySelector('#cc-add-channel')")
        await b.js('window.__traceCh=[]')
        await click(b, '#cc-add-channel')
        await b.until("!!document.querySelector('[data-cc-view=\"git\"]')")
        await b.js("document.querySelector('[data-cc-view=\"git\"]').click()")
        # A real network: the community's control reads are slow, and a refresh of the room is in flight while the
        # channel is created -- it began before the channel existed, so it must not write the list back without it.
        # (The answer is what the relays held WHEN ASKED, arriving late -- not a late question.)
        await b.js("(()=>{const q=__PC.relayQueryFrom;__PC.relayQueryFrom=async(r,f,o)=>{const got=await q(r,f,o);"
                   "if((f||[]).some(x=>(x.kinds||[]).includes(1059)))await new Promise(z=>setTimeout(z,1500));return got;};"
                   "PCConcord.hydrateRoomStreams(__PC,0);})()")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click()")
        await b.until("!!document.querySelector('#ccg-pick-q')")
        await b.js("(()=>{const q=document.querySelector('#ccg-pick-q');q.value='posterchan';q.dispatchEvent(new Event('input'));})()")
        await b.until("!!document.querySelector('#ccg-pick-list [data-ccg-pick=\"'+__coord+'\"]')")
        await b.js("document.querySelector('#ccg-pick-list [data-ccg-pick=\"'+__coord+'\"]').click()")
        await b.until("PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.some(c=>c.name==='posterchanai')")
        await b.until("!!document.querySelector('[data-cc-channel=\"posterchanai\"]')")
        ch = await b.js("PCConcordGit.attachmentsOf(PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='posterchanai')).map(a=>a.address.coordinate)")
        assert ch == [await b.js('__coord')], ch
        t0 = await b.js("performance.now()")
        await b.until("!!document.querySelector('.ccg-chan-badge')")
        print('BADGE after ms', round(await b.js("performance.now()") - t0))
        # The creator lands IN the new channel (a background pass that began before it existed must not move them).
        await asyncio.sleep(1.5)
        print('TRACE', json.dumps(await b.js('__traceCh'), indent=0))
        assert await b.js("!!document.querySelector('[data-cc-channel=\"posterchanai\"].active')"), \
            await b.js("(document.querySelector('[data-cc-channel].active')||{}).textContent")
        # The channel is new, so no activity is inside its interval yet -- it says what it is instead of being blank.
        await b.until("!!document.querySelector('.cc-message-list .ccg-intro') && document.querySelector('.ccg-intro').innerText.includes('posterchanai')")
        intro = await b.js("document.querySelector('.ccg-intro').innerText")
        assert 'appear in this channel from' in intro, intro
        await click(b, '.ccg-intro [data-ccg-intro-open]')
        await b.until("!!document.querySelector('#ccg-projects')")
        assert not [t for t in await b.js('__toasts') if 'not created' in t], await b.js('__toasts')
        assert not await b.js('__errors'), await b.js('__errors')

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
