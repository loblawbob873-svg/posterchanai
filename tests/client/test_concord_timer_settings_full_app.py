"""Real settings controls publish authenticated timer metadata and channel notices."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_channel_controls_full_app import click

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_timer_settings_publish_and_preserve_signed_state(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=1000,deviceScaleFactor=1,mobile=width<600))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord')
        await b.js(r'''(async()=>{
          const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name:'Timer browser',relays:['wss://timer.invalid'],base:'https://timer.invalid',signEvent:__PC.signTemplate});
          const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
          window.__timerToasts=[];__PC.toast=msg=>__timerToasts.push(msg);window.__timerControls=made.events.filter(e=>e.kind===1059);window.__timerBundle=bundle;window.__timerPublished=[];
          const initialTimer=await PosterCordReader.createMetadataWrap(bundle,__timerControls,{message_expiration:60},__PC.me().pubkey,__PC.signTemplate);__timerControls.push(initialTimer.wrap);
          const info=PosterCordReader.inspectControl(bundle,__timerControls);
          const room={name:'Timer browser',communityId:made.communityId,naddr:'timer-browser',url:made.url,channels:info.channels,cord:{bundle,hydrated:true}};
          __PC.relayQuery=async()=>__timerControls;__PC.relayQueryFrom=async()=>__timerControls;
          __PC.relayPublishRoom=async(_relays,event)=>{__timerPublished.push(event);if(event.pubkey===bundle.control_pk)__timerControls.push(event);return {ok:true};};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');PCConcord.render();
        })()''')
        await b.until("JSON.parse(localStorage.getItem('pc.concord.invites'))[0].message_expiration===60")
        await click(b,'#cc-edit-icon')
        await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")
        assert not await b.js("document.querySelector('#cc-message-expiration').disabled")
        await b.js("(()=>{const s=document.querySelector('#cc-message-expiration');s.value='86400';s.dispatchEvent(new Event('change',{bubbles:true}));document.activeElement.blur();PCConcord.backgroundRender();})()")
        await click(b,'#cc-settings-save')
        await b.until('__timerToasts.length>0')
        assert await b.js("JSON.parse(localStorage.getItem('pc.concord.invites'))[0].message_expiration===86400"), await b.js('__timerToasts')
        result=await b.js(r'''(async()=>{
          const info=PosterCordReader.inspectControl(__timerBundle,__timerControls),ch=info.channels[0];
          const messages=await PosterCordReader.inspectChat(__timerBundle,__timerControls,ch.id,__timerPublished.filter(e=>e.pubkey===ch.streamPubkeys[0]));
          return {timer:info.message_expiration,count:__timerPublished.length,notices:messages.messages.map(e=>({kind:e.kind,tags:e.tags}))};
        })()''')
        assert result['timer']==86400 and result['count']==2
        assert result['notices'][0]['kind']==1740
        assert ['timer','86400'] in result['notices'][0]['tags']
        await click(b,'#cc-edit-icon')
        assert await b.js("document.querySelector('#cc-message-expiration').value")=='86400'
        await click(b,'#cc-settings-save')
        await b.until('__timerPublished.length===3')
        assert await b.js('PosterCordReader.inspectControl(__timerBundle,__timerControls).message_expiration')==86400
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra))
