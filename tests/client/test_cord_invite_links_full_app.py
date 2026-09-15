"""Creator link UI renders untrusted labels safely without publishing on open."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_creator_link_review_is_read_only_and_keeps_secrets_hidden(width):
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=850,deviceScaleFactor=1,mobile=width<600))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await browser.js("__PC.switchView('concord')")
        await browser.until("!!window.PCConcord")
        await browser.js('''(async()=>{
const p=__PC,api=await p.cordInviteLinksModule(),context=p.cordDirectContext();
const made=await PosterCord.createCommunity({owner:context.pubkey,name:'Creator fixture',relays:['wss://fixture.invalid'],base:location.origin,signEvent:context.sign});
const bundle=PosterCord.openInvite(made.url,made.events).bundle;
const room={communityId:bundle.community_id,name:'Creator fixture',cord:{bundle:{...bundle,control_root:made.secrets.controlRoot}},channels:[]};
localStorage.setItem('pc.concord.invites',JSON.stringify([room]));
const minted=await api.create(bundle,context,{base:location.origin,label:'<img src=x onerror=alert(1)> creator'});
window.__creatorSecret=minted.entry.signer_sk;
const event=await context.sign({kind:13303,created_at:Math.floor(Date.now()/1000),tags:[],content:await context.encrypt(context.pubkey,JSON.stringify({entries:[minted.entry],tombstones:[]}))});
p.relayQueryFrom=async(relays,filters,options)=>{options.report.ok=relays;return [event];};
window.__creatorWrites=[];p.relayPublishRoom=async(...args)=>{__creatorWrites.push(args);return {ok:true};};
window.__creatorCopies=[];p.copyValue=value=>__creatorCopies.push(value);
await PCConcord.showOwnedInviteLinks(p,room);
})()''')
        await browser.until("!!document.querySelector('#cc-owned-links button')")
        assert '<img src=x onerror=alert(1)> creator' in await browser.js("document.querySelector('#cc-owned-links').textContent")
        assert not await browser.js("!!document.querySelector('#cc-owned-links img')")
        assert await browser.js("__creatorWrites.length===0&&!document.body.innerHTML.includes(__creatorSecret)")
        await click(browser,'#cc-owned-links button')
        assert await browser.js("__creatorCopies.length===1&&__creatorCopies[0].includes('/invite/')&&__creatorWrites.length===0")
        await click(browser,'#cc-owned-links button')
        assert await browser.js("__creatorWrites.length===0")
    init="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))
