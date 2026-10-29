"""Saving a community's banner does not wait for its slowest relay -- git issue "Community Banners Do Not Animate",
Vyram's reply 2026-10-29: "saving still takes a little bit ... it only takes 10 seconds instead of 20".

An earlier fix stopped awaiting the extra copy to every listed relay. Measured here with the REAL transport
(Relay.queryFrom / Relay.publishTo over a fixture WebSocket) and a community listing two relays -- one that answers at
once and one that accepts the connection and never answers (an AUTH that hangs, a tunnel that cannot reach it):
Save waited 10 s for the fresh read of the community's settings (cordQuery's timeout) and then 5 s for the publish
(publishTo's answer timeout) -- 15 s, every second of it on the silent relay. Now the first relay that ANSWERS is the
read and the first that ACCEPTS is the publish, with 1 s more for the others; the save is still confirmed by a real
answer (a relay's EOSE carrying the controls, a relay's OK), and the silent relay is still offered the edition.

Shipped client in Chrome: the banner is picked in Community settings, Save is pressed, and the clock runs to the
"Settings saved" toast.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import MINT, OPEN_ROOM
from tests.client.test_concord_banner_full_app import BLOBS
from tests.client.test_concord_channel_controls_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Two fixture relays behind the real WebSocket door: fast.invalid answers a REQ (stored events + EOSE) and an EVENT
# (OK) within 5 ms; slow.invalid opens and then never says anything. Every frame is logged with its time.
RELAYS = r'''(()=>{
  const Prev=window.WebSocket;window.__wsLog=[];window.__wsStore=window.__wsStore||[];window.__t0=performance.now();
  const log=(...a)=>__wsLog.push([Math.round(performance.now()-window.__t0),...a]);
  const match=(f,ev)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))&&(!f.ids||f.ids.includes(ev.id))
    &&(f.since==null||ev.created_at>=f.since)&&(f.until==null||ev.created_at<=f.until)
    &&Object.keys(f).filter(k=>k[0]==='#').every(k=>(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
  class FixtureRelay extends EventTarget{
    static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
    constructor(url){
      const s=String(url),m=s.match(/[?&]u=([^&]+)/),real=m?decodeURIComponent(m[1]):s;
      if(!/(fast|slow)\.invalid/.test(real))return new Prev(url);
      super();this.url=s;this.kind=/slow/.test(real)?'slow':'fast';this.readyState=0;
      setTimeout(()=>{this.readyState=1;this._fire('open');},5);
    }
    _fire(t,data){const e=t==='message'?new MessageEvent('message',{data:JSON.stringify(data)}):new Event(t);try{this['on'+t]&&this['on'+t](e);}catch(_){}this.dispatchEvent(e);}
    send(raw){const m=JSON.parse(raw);log(this.kind,m[0],m[0]==='EVENT'?m[1].id:m[1]);
      if(this.kind==='slow')return;
      if(m[0]==='REQ'){const id=m[1],fl=m.slice(2);setTimeout(()=>{for(const ev of __wsStore.filter(ev=>fl.some(f=>match(f,ev))))this._fire('message',['EVENT',id,ev]);this._fire('message',['EOSE',id]);},5);}
      if(m[0]==='EVENT'){const ev=m[1];if(!__wsStore.some(x=>x.id===ev.id))__wsStore.push(ev);setTimeout(()=>this._fire('message',['OK',ev.id,true,'']),5);}
    }
    close(){if(this.readyState===3)return;this.readyState=3;this._fire('close');}
  }
  window.WebSocket=FixtureRelay;
})()'''


async def _save_banner(relays):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(RELAYS)
        await b.js(BLOBS)
        await b.js(r'''(()=>{window.__toasts=[];__PC.toast=m=>__toasts.push([performance.now(),String(m)]);
          __PC.uploadBlob=async blob=>{const bytes=new Uint8Array(await blob.arrayBuffer());
            const url='https://blobs.invalid/up-'+(__uploads.push(bytes.length));__blobs.set(url,bytes);return url;};})()''')
        minted = await b.js(MINT.replace('NAME', json.dumps('Quick save'))
                            .replace("relays:['wss://cord.invalid']", 'relays:' + json.dumps(relays))
                            .replace('__relaySeed(made.events);', '__wsStore.push(...made.events);'))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-edit-icon')")
        await click(b, '#cc-edit-icon')
        await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")
        await b.js(r'''(async()=>{const bytes=await __pngBytes(1200,300,'#36c'),dt=new DataTransfer();
          dt.items.add(new File([bytes],'banner.png',{type:'image/png'}));const f=document.querySelector('#cc-banner-file');
          f.files=dt.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()''')
        await b.until("/ready/i.test(document.querySelector('#cc-banner-status')?.textContent||'') && !document.querySelector('#cc-settings-save').disabled")
        await b.js("window.__t0=performance.now();__wsLog.length=0;__toasts.length=0")
        await click(b, '#cc-settings-save')
        for _ in range(400):   # up to 20 s: the old code needed 15
            if await b.js("__toasts.some(t=>/^(Settings saved|community settings could not be saved)/.test(t[1]))"):
                break
            await asyncio.sleep(.05)
        got['toasts'] = await b.js("__toasts.map(t=>[Math.round(t[0]-__t0),t[1]])")
        got['log'] = await b.js("__wsLog.filter(r=>r[2]==='EVENT'||(r[2]==='REQ'&&/^qf/.test(r[3])))")
        got['sheet_closed'] = await b.js("!(typeof PCConcord.communitySettingsOpen==='function'&&PCConcord.communitySettingsOpen())")
        got['banner'] = await b.js("(PosterCordReader.inspectControl(__cordBundle,__wsStore.filter(e=>e.kind===1059))||{}).banner||null")
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    await desktop.with_browser('online', '', check, extra)
    return got


def _saved(got):
    done = [t for t in got['toasts'] if t[1] == 'Settings saved']
    assert done, ('the save never reported done', got)
    assert got['sheet_closed'], ('the settings sheet stayed open', got)
    assert got['banner'] and got['banner']['url'].startswith('https://blobs.invalid/up-'), ('the banner was not published', got)
    return done[0][0]


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_saving_a_banner_with_prompt_relays_is_quick():
    got = asyncio.run(_save_banner(['wss://fast.invalid']))
    ms = _saved(got)
    print('save reported done after %d ms' % ms)
    assert ms < 1500, ('saving took %d ms with a relay that answers at once' % ms, got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_silent_relay_does_not_hold_the_save():
    got = asyncio.run(_save_banner(['wss://fast.invalid', 'wss://slow.invalid']))
    ms = _saved(got)
    print('save reported done after %d ms' % ms)
    # The removed waits: 10 s for the read and 5 s for the publish, both on the relay that never answers.
    assert ms < 4000, ('Save waited for the relay that never answers: %d ms' % ms, got)
    sent = {r[1] for r in got['log'] if r[2] == 'EVENT'}
    assert {'fast', 'slow'} <= sent, ('the edition must still be offered to every listed relay', got)
