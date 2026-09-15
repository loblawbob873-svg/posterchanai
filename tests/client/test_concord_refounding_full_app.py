"""Real bundled form: explicit access review, validation and cancellation."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_refounding_requires_review_and_preserves_private_access_boundaries():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until("!!window.PCConcord?.reviewRefoundingRecipients")
        await b.js("""(()=>{
          window.__refoundRoom={local:true,name:'Review fixture',channels:[],cord:{bundle:{owner:__PC.me().pubkey,channels:[{id:'a'.repeat(64),name:'staff'}]}}};
          window.__refoundResult='pending';void PCConcord.reviewRefoundingRecipients(__PC,__refoundRoom).then(r=>__refoundResult=r);
        })()""")
        await b.until("!!document.querySelector('[aria-label=\"Review community access\"]')")
        assert await b.js("document.querySelector('[aria-label=\"Review community access\"] .btn-primary').disabled")
        private = await b.js("document.querySelector('textarea[aria-label=\"Keep access to #staff\"]').value")
        assert private == await b.js("NostrTools.nip19.npubEncode(__PC.me().pubkey)")
        await b.js("document.querySelector('[aria-label=\"Review community access\"] .btn-ghost').click()")
        await b.until("window.__refoundResult===null")
        await b.js("window.__refoundResult='pending';void PCConcord.reviewRefoundingRecipients(__PC,__refoundRoom).then(r=>__refoundResult=r)")
        await b.js("""(()=>{const d=document.querySelector('[aria-label="Review community access"]');d.querySelector('textarea').value='not-a-public-address';d.querySelector('input[type=checkbox]').click();d.querySelector('.btn-primary').click();})()""")
        assert await b.js("!!document.querySelector('[aria-label=\"Review community access\"] [role=alert]').textContent")
        assert await b.js("window.__refoundResult") == 'pending'
        await b.js("""(()=>{const d=document.querySelector('[aria-label="Review community access"]');d.querySelector('textarea').value=NostrTools.nip19.npubEncode(__PC.me().pubkey);d.querySelector('.btn-primary').click();})()""")
        await b.until("window.__refoundResult!=='pending'")
        assert await b.js("__refoundResult.recipients[0].pubkey===__PC.me().pubkey")
        assert await b.js("__refoundResult.channelRecipients['a'.repeat(64)][0].pubkey===__PC.me().pubkey")
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_owner_can_open_and_cancel_rotation_from_actual_settings(width):
    from tests.client.test_concord_channel_controls_full_app import click

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width<600))
        await desktop.login(b)
        await b.js("""(()=>{
          const room={name:'Rotation fixture',communityId:'c'.repeat(64),naddr:'rotation-fixture',channels:[{id:'a'.repeat(64),name:'staff'}],cord:{bundle:{owner:__PC.me().pubkey,channels:[{id:'a'.repeat(64),name:'staff'}],relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'a'.repeat(64),name:'staff',streamPubkeys:[]}]}),inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
          __PC.switchMessagesTab('concord');
        })()""")
        await b.until("!!document.querySelector('#cc-refound')")
        await click(b, '#cc-edit-icon')
        await click(b, '#cc-refound')
        await b.until("!!document.querySelector('[aria-label=\"Review community access\"]')")
        await click(b, '[aria-label="Review community access"] .btn-ghost')
        await b.until("!document.querySelector('[aria-label=\"Review community access\"]')")
        assert await b.js("!JSON.parse(localStorage.getItem('pc.concord.invites'))[0].cord.refounding")
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra))
