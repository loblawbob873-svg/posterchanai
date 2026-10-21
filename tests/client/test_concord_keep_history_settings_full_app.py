"""Concord → community settings → Keep history: a per-community setting, saved the way CORD-02 §6 says.

The real settings sheet: choosing 30 days publishes ONE metadata edition whose `custom` carries
`posterchan/history_retention: 30` beside another client's `rules` (round-tripped, never replaced), the saved
room follows it, nothing is posted into the channels (it is not a timer -- no 1740 notice), and choosing
Forever removes only our key. docs/CONCORD_RETENTION.md.
"""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_channel_controls_full_app import click

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

KEY = 'posterchan/history_retention'

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_keep_history_is_a_community_setting_stored_in_custom(width):
    got = {}
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=1000,deviceScaleFactor=1,mobile=width<600))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord')
        await b.js(r'''(async()=>{
          const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name:'Timer browser',relays:['wss://timer.invalid'],base:'https://timer.invalid',signEvent:__PC.signTemplate});
          const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
          __PC.sendCordDirectInvite=async()=>{throw Error('unexpected key delivery in timer test');};window.__timerToasts=[];__PC.toast=msg=>__timerToasts.push(msg);window.__timerControls=made.events.filter(e=>e.kind===1059);window.__timerBundle=bundle;window.__timerPublished=[];
          const initialTimer=await PosterCordReader.createMetadataWrap(bundle,__timerControls,{message_expiration:60,customPatch:{rules:'Be excellent to each other.'}},__PC.me().pubkey,__PC.signTemplate);__timerControls.push(initialTimer.wrap);
          const info=PosterCordReader.inspectControl(bundle,__timerControls);
          const room={name:'Timer browser',communityId:made.communityId,naddr:'timer-browser',url:made.url,channels:info.channels,cord:{bundle,hydrated:true}};
          __PC.relayQuery=async()=>__timerControls;__PC.relayQueryFrom=async()=>__timerControls;
          __PC.relayPublishRoom=async(_relays,event)=>{__timerPublished.push(event);if(event.pubkey===bundle.control_pk)__timerControls.push(event);return {ok:true};};
          localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');PCConcord.render();
        })()''')
        await b.until("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey))[0].message_expiration===60")
        await b.until("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey))[0].message_expiration===60")
        async def choose(value):
            await click(b,'#cc-edit-icon')
            await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")
            got.setdefault('default', await b.js("document.querySelector('#cc-history-retention').value"))
            got.setdefault('options', await b.js("[...document.querySelectorAll('#cc-history-retention option')].map(o=>o.textContent)"))
            n = await b.js('__timerPublished.length')
            await b.js("(()=>{const s=document.querySelector('#cc-history-retention');s.value='%s';s.dispatchEvent(new Event('change',{bubbles:true}));})()" % value)
            await click(b,'#cc-settings-save')
            await b.until('__timerPublished.length>%d' % n)
            await asyncio.sleep(.4)
            return await b.js("(()=>{const info=PosterCordReader.inspectControl(__timerBundle,__timerControls);"
                              "return {custom:info.custom,timer:info.message_expiration,published:__timerPublished.length-%d,"
                              "room:JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey))[0].history_retention};})()" % n)
        got['thirty'] = await choose('30')
        got['forever'] = await choose('0')
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra))
    assert got['default'] == '0' and got['options'][0] == 'Forever', got
    t = got['thirty']
    assert t['custom'] == {'rules': 'Be excellent to each other.', KEY: 30}, ("our key must sit beside the other client's", t)
    assert t['published'] == 1, ("Keep history is not a timer: one metadata edition, no channel notices", t)
    assert t['timer'] == 60 and t['room'] == 30, t
    f = got['forever']
    assert f['custom'] == {'rules': 'Be excellent to each other.'} and f['room'] == 0, ("Forever must remove only our key", f)
