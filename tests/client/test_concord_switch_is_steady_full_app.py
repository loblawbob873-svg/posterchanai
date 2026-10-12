"""Switching room or community in Concord holds still.

Reported from the APK: "concord shakes when changing room/community". Measured frame by frame (every
frame sampled AFTER it was painted, a layout-shift observer running, animations and transitions
logged) in the real bundle, with two REAL minted CORD communities on an in-page relay with 300ms of
latency, encrypted pictures that decrypt 250ms after they are asked for (half of them with an imeta
`dim`, half without, as older messages have), at 390x844 as the APK (Capacitor present, touch, an
Android UA, DPR 2) and at 1280x800. What moved, before the fix:

  * PHONE, community -> community from inside the drawer: the whole drawer -- rail and channel list --
    slid in from -28px AGAIN on every render while it stood open (`ccDrawerIn` was keyed on the
    `drawer-open` STATE, which every rebuild carries), and a switch renders two or three times (the
    switch, the cached history, the network history). Measured: the channel list at x=30,35,...,58
    twice per switch. That is the shake.
  * Opening a channel: a picture landing replaced its placeholder in a promise task and the scroller
    was put back by a ResizeObserver a frame later, so the newest messages jumped 154px up and back:
    a layout shift of 0.15 (phone) / 0.066 (desktop) on an ordinary room change.
  * DESKTOP: the switched-to channel's first frame was drawn 136px above its newest message (the old
    channel's offset was carried into the new one, keyed on a scroll key computed AFTER the switch),
    then jumped down; a second jump followed at ~1s.
  * The active channel's glow pulse restarted from its first keyframe on every rebuild.

What is asserted, per switch: cumulative layout shift < 0.02; the conversation header and the
composer never move; the drawer never slides while it is merely redrawn; the new channel lands at its
newest message in ONE step and stays there while its pictures arrive (distance from the bottom is
0 in every painted frame); the glow keeps its phase; a plain picture with `dim` holds its box before
it loads. The fixture is the real client end to end -- only the relay and the blob server are fakes.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The APK's native surface, as the other APK-path full-app tests stub it.
APK = r'''
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{
 App:{addListener:()=>({remove(){}})},
 HomeScreen:{consumeLaunchView:async()=>({view:''}),addListener:()=>({remove(){}}),formFactor:async()=>({form:'phone'})}}};
'''
ANDROID_UA = ('Mozilla/5.0 (Linux; Android 14; Pixel 7 Build/UQ1A; wv) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36')
NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

# A blob server for encrypted pictures: answers 250ms after it is asked, like a real one.
IMAGES = r'''(()=>{window.__blobs=new Map();
 const f0=window.fetch;window.fetch=(u,o)=>{const k=String(u&&u.url||u);return __blobs.has(k)?new Promise(r=>setTimeout(()=>r(new Response(__blobs.get(k).slice(0))),250)):f0(u,o);};
 window.__hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
 window.__sealedImage=async(w,h,color,withDim)=>{const c=new OffscreenCanvas(w,h),g=c.getContext('2d');g.fillStyle=color;g.fillRect(0,0,w,h);
  const plain=new Uint8Array(await (await c.convertToBlob({type:'image/png'})).arrayBuffer());
  const raw=crypto.getRandomValues(new Uint8Array(32)),iv=crypto.getRandomValues(new Uint8Array(16));
  const key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt']);
  const cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,plain);const ox=__hex(await crypto.subtle.digest('SHA-256',plain));
  const url='https://blossom.fixture.invalid/'+__hex(await crypto.subtle.digest('SHA-256',cipher));__blobs.set(url,cipher);
  const t=['imeta','url '+url,'m image/png','encryption-algorithm aes-gcm','decryption-key '+__hex(raw),'decryption-nonce '+__hex(iv),'ox '+ox,'name pic.png'];
  if(withDim)t.push('dim '+w+'x'+h);return t;};
})()'''

LATENCY = r'''(()=>{const d=ms=>new Promise(r=>setTimeout(r,ms));
 const q=__PC.relayQuery,qf=__PC.relayQueryFrom;__PC.relayQuery=async(...a)=>{await d(window.__lat||0);return q(...a);};
 __PC.relayQueryFrom=async(...a)=>{await d(window.__lat||0);return qf(...a);};})()'''

# Two real communities, each with #general and #random, messages of differing heights, a picture
# every seventh message (alternately with and without `dim`), and one PLAIN picture with `dim`.
BUILD = r'''(async()=>{
 const me=__PC.me().pubkey,rooms=[],counts={};
 for(const [n,name] of [['a','Alpha'],['b','Beta']]){
  const made=await PosterCord.createCommunity({owner:me,name,relays:['wss://cord.invalid'],base:'https://cord.invalid',signEvent:__PC.signTemplate});
  const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
  __relaySeed(made.events);
  const ctl=()=>__relayStore.filter(e=>e.kind===1059&&PosterCordReader.inspectControl(bundle,[]).controlPubkeys.includes(e.pubkey));
  const ch=await PosterCordReader.createChannelWrap(bundle,ctl(),{name:'random'},me,__PC.signTemplate);__relaySeed([ch.wrap]);
  const info=PosterCordReader.inspectControl(bundle,ctl());
  for(const c of info.channels){const count=c.name==='general'?(n==='a'?45:30):(n==='a'?25:40);counts[name+'/'+c.name]=count;
   for(let i=0;i<count;i++){
    let tags=i%7===3?[await __sealedImage(400+i*10,300,'#'+((i*40)%255).toString(16).padStart(2,'0')+'8844',i%2===0)]:[];
    if(n==='a'&&c.name==='random'&&i===count-2)tags=[['imeta','url https://plain.fixture.invalid/p.png','m image/png','dim 640x480','name plain.png']];
    const w=await PosterCordReader.createChatWrap(bundle,ctl(),c.id,name+' #'+c.name+' message '+i+(i%4?'':' with a longer line that wraps on a phone so rows have different heights'),me,__PC.signTemplate,tags);
    __relaySeed([w.wrap]);}}
  rooms.push({name,communityId:made.communityId,naddr:'cord-'+n,url:made.url,channels:info.channels,cord:{bundle,hydrated:true}});
 }
 localStorage.setItem('pc.concord.rooms.v1.'+me,JSON.stringify(rooms));localStorage.setItem('pc.concord.active.v1.'+me,'0');
 window.__counts=counts;return counts;
})()'''

# Every frame, sampled in a task queued from requestAnimationFrame -- i.e. after that frame's style,
# layout and ResizeObserver work, which is what was actually painted. Plus the layout-shift entries
# and the drawer's position.
RECORD = r'''window.__record=(ms,want)=>{let landedAt=null;window.__rec=null;return new Promise(res=>{
 const sel={header:'.cc-conversation > header',compose:'.cc-compose',drawer:'.cc-channels'};
 const frames=[],shifts=[],t0=performance.now();
 const po=new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)shifts.push({v:e.value,t:Math.round(e.startTime-t0)});});
 po.observe({type:'layout-shift',buffered:false});
 const pulse=()=>{const a=document.querySelector('.cc-channel.active');const an=a&&a.getAnimations?a.getAnimations().find(x=>x.animationName==='ccThemePulse'):null;
   if(!an||an.playState!=='running')return null;const ct=an.effect.getComputedTiming();const beat=(Number(document.timeline.currentTime)%3200)/3200;
   let d=Math.abs((ct.progress||0)-beat);return Math.min(d,1-d);};
 const mc=new MessageChannel();
 mc.port1.onmessage=()=>{const f={t:Math.round(performance.now()-t0)};
  for(const[k,s]of Object.entries(sel)){const el=document.querySelector(s);if(!el){f[k]=null;continue;}const r=el.getBoundingClientRect();f[k]=r.width?[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]:null;}
  const m=document.querySelector('.cc-messages');
  if(m){f.top=Math.round(m.scrollTop);f.fromBottom=Math.round(m.scrollHeight-m.clientHeight-m.scrollTop);f.n=m.querySelectorAll('.cc-message').length;f.channel=(document.querySelector('.cc-conversation > header b')||{}).textContent;}
  f.pulse=pulse();f.open=!!document.querySelector('.cc-app.drawer-open');frames.push(f);
  /* At least `ms`; when a channel is expected, until it has been on screen for 1.2s (a cold
     community's history is a relay round trip away), but never past 10s. */
  if(want&&landedAt==null&&f.channel===want[0]&&f.n===want[1])landedAt=f.t;
  const el=performance.now()-t0,more=el<ms||(want&&(landedAt==null?el<10000:f.t-landedAt<1200));
  if(more)requestAnimationFrame(()=>mc.port2.postMessage(0));
  else{po.disconnect();res({frames,cls:shifts.reduce((a,s)=>a+s.v,0),shifts});}};
 requestAnimationFrame(()=>mc.port2.postMessage(0));
}).then(r=>window.__rec=r);}'''


async def tap(b, selector, phone):
    """A real tap (touch on the phone, mouse on the desktop) at the element's centre."""
    xy = await b.js(f"(()=>{{const r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}})()")
    if phone:
        pt = [{'x': xy[0], 'y': xy[1]}]
        await b.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': pt})
        await b.call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    else:
        for kind in ('mousePressed', 'mouseReleased'):
            await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': xy[0], 'y': xy[1], 'button': 'left', 'clickCount': 1})


async def switch(b, selector, phone, ms=1500, want=None):
    await b.js(RECORD)
    await b.js("(__record(%d,%s),0)" % (ms, json.dumps(want)))
    await tap(b, selector, phone)
    for _ in range(150):
        if await b.js('!!window.__rec'):
            break
        await asyncio.sleep(.1)
    return await b.js('window.__rec')


def problems(label, rec, *, expect=None, drawer_open=False):
    """Everything about one switch that moved and should not have, as readable lines."""
    out = []
    frames = rec['frames']
    if rec['cls'] >= 0.02:
        out.append(f"{label}: cumulative layout shift {rec['cls']:.4f} {rec['shifts']}")
    for part in ('header', 'compose'):
        seen = sorted({json.dumps(f[part]) for f in frames if f.get(part)})
        if len(seen) > 1:
            out.append(f'{label}: the {part} moved: {seen}')
    if drawer_open:
        xs = [f['drawer'][0] for f in frames if f.get('open') and f.get('drawer')]
        if len(set(xs)) > 1:
            out.append(f'{label}: the open drawer slid while it was redrawn: x={xs}')
    if expect:
        landed = [f for f in frames if f.get('channel') == expect[0] and f.get('n') == expect[1]]
        if not landed:
            out.append(f'{label}: never showed {expect} ({frames[-1]})')
        else:
            off = [(f['t'], f['fromBottom']) for f in landed if f['fromBottom'] > 2]
            if off:
                out.append(f'{label}: not at the newest message in a painted frame (ms, px above): {off}')
    phases = [f['pulse'] for f in frames if f.get('pulse') is not None]
    if phases and max(phases) > 0.06:
        out.append(f'{label}: the active channel glow restarted (phase off by {max(phases):.2f} of a beat)')
    return out


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_switching_room_and_community_holds_still(width):
    phone = width < 600

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844 if phone else 800,
                                                               deviceScaleFactor=2 if phone else 1, mobile=phone))
        if phone:
            await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
            await b.call('Emulation.setUserAgentOverride', {'userAgent': ANDROID_UA, 'platform': 'Linux armv8l'})
        await desktop.login(b)
        await b.js(NO_OS)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        await b.js(LATENCY)
        await b.js(IMAGES)
        counts = await b.js(BUILD)
        await b.js('window.__lat=300')
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('[data-cc-server=\"0\"]')")
        await b.js("document.querySelector('[data-cc-server=\"0\"]').click()")
        await b.until("!!document.querySelector('[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/general']}")
        await asyncio.sleep(3)          # the first room's history, pictures and live sync settle

        async def open_drawer():
            if phone and not await b.js("!!document.querySelector('.cc-app.drawer-open')"):
                await tap(b, '#cc-back-channels', phone)
                await b.until("!!document.querySelector('.cc-app.drawer-open')")
                await asyncio.sleep(.5)  # the drawer's own opening slide is not part of a switch

        found = []
        # 1. Room -> room: #general -> #random in Alpha (first visit; its pictures decrypt on screen).
        await open_drawer()
        rec = await switch(b, '[data-cc-channel="random"]', phone, want=['random', counts['Alpha/random']])
        found += problems('room->room', rec, expect=('random', counts['Alpha/random']))
        plain = await b.js("""(()=>{const d=document.querySelector('.cc-plain-attachment');if(!d)return null;
          const r=d.getBoundingClientRect();return {sized:d.hasAttribute('data-cc-sized'),w:Math.round(r.width),h:Math.round(r.height)};})()""")
        if not plain or not plain['sized'] or abs(plain['h'] - plain['w'] * 480 / 640) > 2:
            found.append(f'a plain picture with dim 640x480 did not reserve its box: {plain}')
        await asyncio.sleep(2)
        # 2. Community -> community: Alpha -> Beta (never opened, so its history comes from the relay).
        await open_drawer()
        beta = ['general', counts['Beta/general']]
        rec = await switch(b, '[data-cc-server="1"]', phone, want=None if phone else beta)
        if phone:     # the switch happens inside the open drawer; the channel tap shows the room
            found += problems('community->community (drawer)', rec, drawer_open=True)
            await asyncio.sleep(1.5)
            rec = await switch(b, '[data-cc-channel="general"]', phone, want=beta)
        found += problems('community->community', rec, expect=('general', counts['Beta/general']))
        await asyncio.sleep(2)
        # 3. And back to a community this device has already read (painted from what it holds).
        await open_drawer()
        alpha = ['general', counts['Alpha/general']]
        rec = await switch(b, '[data-cc-server="0"]', phone, want=None if phone else alpha)
        if phone:
            found += problems('community back (drawer)', rec, drawer_open=True)
            await asyncio.sleep(1.5)
            rec = await switch(b, '[data-cc-channel="general"]', phone, want=alpha)
        found += problems('community back', rec, expect=('general', counts['Alpha/general']))
        assert await b.js('__errors') == []
        assert not found, '\n'.join(found)

    asyncio.run(desktop.with_browser('online', '', check, extra_init=APK if phone else ''))
