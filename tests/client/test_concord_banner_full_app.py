"""A community's BANNER (CORD-02 metadata `banner`, an encrypted ImagePointer like `icon`) is shown,
and its owner can set one.

Reported by a member: "I see no banners for community." Armada sets the banner in Community
settings (AES-256-GCM, 16-byte nonce, `{url,key,nonce,hash}`), and every member's client is meant
to draw it above the channel list. Real minted community, real reader, real signer, real DOM; the
relay and the blob server are in-page fixtures (tests/client/concord_fixture_relay.py).
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM, CONTROLS
from tests.client.test_concord_channel_controls_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# A fixture blob server: what `uploadBlob` stores, `fetch` serves back, byte for byte.
BLOBS = r'''
window.__blobs=new Map();window.__uploads=[];
{const inner=window.fetch;window.fetch=function(url,opts){const u=String(url&&url.url||url);
  if(__blobs.has(u))return Promise.resolve(new Response(__blobs.get(u),{status:200,headers:{'Content-Type':'application/octet-stream'}}));
  return inner.apply(this,arguments);};}
window.__pngBytes=async(w,h,color)=>{const c=new OffscreenCanvas(w,h),g=c.getContext('2d');g.fillStyle=color;g.fillRect(0,0,w,h);
  return new Uint8Array(await (await c.convertToBlob({type:'image/png'})).arrayBuffer());};
window.__hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
window.__sealPointer=async plain=>{const key=crypto.getRandomValues(new Uint8Array(32)),nonce=crypto.getRandomValues(new Uint8Array(16));
  const k=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['encrypt']);
  const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},k,plain));
  const url='https://blobs.invalid/'+__hex(await crypto.subtle.digest('SHA-256',cipher));__blobs.set(url,cipher);
  return {url,key:__hex(key),nonce:__hex(nonce),hash:__hex(await crypto.subtle.digest('SHA-256',plain))};};
'''

BANNER_SHOWN = """(()=>{const img=document.querySelector('.cc-channels .cc-banner img');if(!img)return false;
  const r=img.getBoundingClientRect();return img.complete&&img.naturalWidth>0&&r.width>100&&r.height>20;})()"""


async def setup(b, width, name):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js("__PC.switchMessagesTab('concord')")
    await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
    await b.js(INSTALL)
    await b.js(BLOBS)
    return await b.js(MINT.replace('NAME', json.dumps(name)))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_a_banner_set_elsewhere_is_shown_to_members(width):
    """The reported case: the banner was set by the owner (as Armada does) and this device has
    never seen it. Opening the community must show it above the channel list, and keep showing it
    across the metadata refresh ticks that follow (a thin read is not "the banner was removed")."""
    async def check(b):
        minted = await setup(b, width, 'Banner seen')
        await b.js(r'''(async()=>{
          const pointer=await __sealPointer(await __pngBytes(900,240,'#c33'));
          const made=await PosterCordReader.createMetadataWrap(__cordBundle,'''+CONTROLS+''',{bannerRef:pointer},__PC.me().pubkey,__PC.signTemplate);
          __relaySeed([made.wrap]);window.__bannerPointer=pointer;
        })()''')
        # The banner is in the community's control history -- read it the way Armada would.
        info = await b.js('PosterCordReader.inspectControl(__cordBundle,' + CONTROLS + ').banner')
        assert info and info['url'].startswith('https://blobs.invalid/') and len(info['nonce']) == 32
        room = minted['room']
        room['cord']['hydrated'] = False
        await b.js(OPEN_ROOM + '(' + json.dumps(room) + ')')
        await b.until(BANNER_SHOWN)
        # It stays through the live refresh ticks (4 s each) -- a pass that saw no metadata must
        # not take the banner away.
        for _ in range(6):
            await asyncio.sleep(1)
            assert await b.js(BANNER_SHOWN), 'the banner disappeared on a metadata refresh'
        # A THIN READ: the envelope cache answers [] (another window holding the database is
        # enough) and the relays answer nothing. That is "could not read the metadata", never "the
        # owner removed the banner" -- the icon already follows this rule; the banner must too.
        # Not even for one frame: every DOM change is checked, so a pass that removes the banner and a
        # later one that restores it still counts as the flicker members saw.
        await b.js("window.__bannerGone=0;new MutationObserver(()=>{if(!document.querySelector('.cc-channels .cc-banner img'))__bannerGone++;})"
                   ".observe(document.body,{childList:true,subtree:true});"
                   "PCConcordCache.get=async()=>[];PCConcordCache.page=async()=>({events:[]});"
                   "__PC.relayQuery=async()=>[];__PC.relayQueryFrom=async()=>[];")
        for _ in range(10):
            await asyncio.sleep(1)
            assert await b.js(BANNER_SHOWN), 'a metadata pass that read nothing took the banner away'
        assert await b.js('__bannerGone') == 0, 'a metadata pass that read nothing removed the banner (it came back later)'
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_the_owner_sets_a_banner_with_visible_upload_progress(width):
    """Community settings offers a banner upload; the upload shows its progress, Save publishes the
    CORD-02 `banner` ImagePointer (16-byte nonce, Armada's shape) and the banner appears."""
    async def check(b):
        minted = await setup(b, width, 'Banner owner')
        await b.js(r'''(()=>{window.__toasts=[];__PC.toast=m=>__toasts.push(String(m));
          window.__releaseUpload=null;
          __PC.uploadBlob=async(blob,opts={})=>{const bytes=new Uint8Array(await blob.arrayBuffer());__uploads.push({size:bytes.length,opts:Object.keys(opts)});
            if(opts.onProgress)opts.onProgress({loaded:Math.floor(bytes.length/2),total:bytes.length});
            await new Promise(r=>{window.__releaseUpload=r;});
            if(opts.onProgress)opts.onProgress({loaded:bytes.length,total:bytes.length});
            const url='https://blobs.invalid/up-'+__uploads.length;__blobs.set(url,bytes);return url;};
        })()''')
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-edit-icon')")
        await click(b, '#cc-edit-icon')
        await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")
        assert await b.js("!!document.querySelector('#cc-banner-file')"), 'settings offer no banner upload'
        await b.js(r'''(async()=>{const bytes=await __pngBytes(1200,300,'#36c'),dt=new DataTransfer();
          dt.items.add(new File([bytes],'banner.png',{type:'image/png'}));const f=document.querySelector('#cc-banner-file');
          f.files=dt.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()''')
        # Progress is visible while the upload runs, and Save waits for it.
        await b.until("/\\d+%/.test(document.querySelector('#cc-banner-status')?.textContent||'')")
        assert await b.js("document.querySelector('#cc-settings-save').disabled"), 'Save is live while the banner uploads'
        await b.js('__releaseUpload()')
        await b.until("/ready|uploaded/i.test(document.querySelector('#cc-banner-status')?.textContent||'')")
        await click(b, '#cc-settings-save')
        await b.until(BANNER_SHOWN)
        pointer = await b.js('PosterCordReader.inspectControl(__cordBundle,' + CONTROLS + ').banner')
        assert pointer['url'] == 'https://blobs.invalid/up-1'
        assert len(pointer['nonce']) == 32 and len(pointer['key']) == 64 and len(pointer['hash']) == 64
        # The pointer decrypts to the image that was picked (a fresh reader of the same bytes).
        ok = await b.js(r'''(async()=>{const p=PosterCordReader.inspectControl(__cordBundle,''' + CONTROLS + r''').banner;
          const h=s=>Uint8Array.from(s.match(/../g),x=>parseInt(x,16));
          const k=await crypto.subtle.importKey('raw',h(p.key),'AES-GCM',false,['decrypt']);
          const plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:h(p.nonce)},k,__blobs.get(p.url)));
          return __hex(await crypto.subtle.digest("SHA-256",plain))===p.hash&&(plain[0]===0x89||plain[0]===0xff);})()''')
        assert ok
        # Editing the name later keeps the banner (round-trip of the metadata body).
        await b.js(r'''(async()=>{const made=await PosterCordReader.createMetadataWrap(__cordBundle,''' + CONTROLS + r''',{description:'still bannered'},__PC.me().pubkey,__PC.signTemplate);__relaySeed([made.wrap]);})()''')
        assert (await b.js('PosterCordReader.inspectControl(__cordBundle,' + CONTROLS + ').banner'))['url'] == 'https://blobs.invalid/up-1'
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_banner_only_save_keeps_the_communitys_other_settings():
    """2026-10-15: "uploading banner blew away all the community settings". Save sent EVERY field -- this
    device's copy of the name, the description box, the icon box -- so a sheet that opened without the
    community's current description/icon (set on another device) wrote a blank description and "no icon"
    over the real ones. Only changed fields go out now, on a fresh read of the community's controls."""
    async def check(b):
        minted = await setup(b, 1280, 'Settings keeper')
        await b.js(r'''(async()=>{const made=await PosterCordReader.createMetadataWrap(__cordBundle,''' + CONTROLS + r''',
          {description:'Our place on the network',icon:'🌌'},__PC.me().pubkey,__PC.signTemplate);__relaySeed([made.wrap]);})()''')
        await b.js(r'''(()=>{__PC.uploadBlob=async(blob)=>{const bytes=new Uint8Array(await blob.arrayBuffer());
          const url='https://blobs.invalid/keep-'+(__uploads.push(1));__blobs.set(url,bytes);return url;};})()''')
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-edit-icon')")
        await click(b, '#cc-edit-icon')
        await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")
        # This device opened the sheet WITHOUT the community's current description and icon.
        await b.js(r'''(()=>{for(const id of ['#cc-description-value','#cc-settings-icon']){const el=document.querySelector(id);
          el.defaultValue='';el.value='';}})()''')
        await b.js(r'''(async()=>{const bytes=await __pngBytes(1200,300,'#c3c'),dt=new DataTransfer();
          dt.items.add(new File([bytes],'banner.png',{type:'image/png'}));const f=document.querySelector('#cc-banner-file');
          f.files=dt.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()''')
        await b.until("/ready|uploaded/i.test(document.querySelector('#cc-banner-status')?.textContent||'')")
        await click(b, '#cc-settings-save')
        await b.until(BANNER_SHOWN)
        meta = await b.js('(()=>{const m=PosterCordReader.inspectControl(__cordBundle,' + CONTROLS + ');'
                          'return {name:m.name,description:m.description,picture:m.picture||m.icon||"",banner:!!m.banner}})()')
        assert meta['banner'], meta
        assert meta['description'] == 'Our place on the network', ('the banner save wiped the description', meta)
        assert meta['picture'] == '🌌', ('the banner save wiped the icon', meta)
        assert meta['name'] == 'Settings keeper', ('the banner save changed the name', meta)
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_save_after_a_forked_history_still_changes_the_community():
    """PosterChan, 2026-10-15, measured on the owner's desktop: a client that read NO prior metadata (its local
    cache answered empty) wrote a fresh v1, forking the edition chain. The fold keeps the lowest rumor id per
    version, so it anchored on one v1 while the v2 slot went to the original v2 -- a gap -- and every save after
    that (v2, linked to the anchored v1) lost the tie-break: "says saved, does not show", ten times. A new
    edition now takes the lowest id in its slot, so the save lands, in this client and in every other."""
    async def check(b):
        minted = await setup(b, 1280, 'Forked keeper')
        # v2: a real icon and description, saved normally.
        await b.js(r'''(async()=>{const made=await PosterCordReader.createMetadataWrap(__cordBundle,''' + CONTROLS + r''',
          {description:'original',icon:'🌌'},__PC.me().pubkey,__PC.signTemplate);__relaySeed([made.wrap]);})()''')
        # The fork: saves written from an EMPTY read are fresh v1s; repeat until one of them sorts below the
        # creation v1 (the shape PosterChan has: the rogue v1 anchors, the original v2 owns the next slot).
        # UP TO 4000 TRIES, NOT 40. Each try sorts below with p = (the creation v1's id) / 2^256, uniform -- so the
        # chance n tries all miss averages 1/(n+1): 1 run in 41 at 40 (3 of 8 desktop CI runs failed here, 2026-10-27),
        # 1 in 4001 now. Measured: 300 tries take 0.5 s, and the loop stops at the first that lands.
        got = await b.js(r'''(async()=>{const C=()=>''' + CONTROLS + r''';let n=0;
          for(;n<4000;n++){const made=await PosterCordReader.createMetadataWrap(__cordBundle,[],{description:'WIPED'},__PC.me().pubkey,__PC.signTemplate);
            __relaySeed([made.wrap]);if(PosterCordReader.inspectControl(__cordBundle,C()).description==='WIPED')break;}
          return {n,desc:PosterCordReader.inspectControl(__cordBundle,C()).description}})()''')
        assert got['desc'] == 'WIPED', ('could not reproduce the fork', got)
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        res = await b.js(r'''(async()=>{const me=__PC.me().pubkey,room=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+me)||'[]').find(r=>r.name==='Forked keeper');
          let err='';try{await PCConcord.saveCommunitySettings(__PC,room,{description:'after the fork'});}catch(e){err=String(e.message||e);}
          return {err,desc:PosterCordReader.inspectControl(__cordBundle,''' + CONTROLS + r''').description}})()''')
        assert res['err'] == '' and res['desc'] == 'after the fork', ('a save after the fork changed nothing', res)
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_settings_save_reaches_every_relay_the_community_lists():
    """PosterChan, 2026-10-15: this node's relay answered OK once ANY one upstream took a settings edition --
    for PosterChan that was a relay the community does not list -- so a refresh that read the community's own
    relays never saw the new icon or banner, and each showed and vanished in turn. A settings save now also
    goes to every listed relay."""
    async def check(b):
        minted = await setup(b, 1280, 'Everywhere keeper')
        await b.js("window.__direct=[];__PC.relayPublishTo=async(relays,ev)=>{__direct.push({relays,id:ev.id});return {ok:true};};true")
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        res = await b.js(r'''(async()=>{const me=__PC.me().pubkey,room=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+me)||'[]').find(r=>r.name==='Everywhere keeper');
          await PCConcord.saveCommunitySettings(__PC,room,{description:'seen everywhere'});
          return {direct:__direct, relays:PCConcord.roomRelays(room.cord.bundle)}})()''')
        assert res['direct'], ('the settings edition went only to this node\'s relay', res)
        assert sorted(res['direct'][-1]['relays']) == sorted(res['relays']), res
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_settings_sheet_shows_the_icon_the_community_has():
    """"community icon missing from community settings": an uploaded icon is encrypted and has no text form, so
    the sheet's Icon box was blank over a community whose icon was set. The sheet now draws the current icon."""
    async def check(b):
        minted = await setup(b, 1280, 'Icon keeper')
        await b.js(r'''(async()=>{const made=await PosterCordReader.createMetadataWrap(__cordBundle,''' + CONTROLS + r''',
          {icon:'🌌'},__PC.me().pubkey,__PC.signTemplate);__relaySeed([made.wrap]);})()''')
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        # The icon is on the rail first (the room loaded its settings), then the sheet is opened.
        await b.until("[...document.querySelectorAll('[data-cc-server]')].some(x=>x.textContent.includes('🌌'))")
        await b.until("!!document.querySelector('#cc-edit-icon')")
        await click(b, '#cc-edit-icon')
        await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")
        await b.until("!!document.querySelector('#cc-icon-current')")
        cur = await b.js("document.querySelector('#cc-icon-current').textContent.trim()||(document.querySelector('#cc-icon-current img')?'img':'')")
        assert cur == '🌌', ('the sheet does not show the community icon', cur)
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_slow_relay_does_not_hold_up_the_save():
    """Git issue "Community Banners Do Not Animate", Vyram's reply 2026-10-27: saving with an avatar or banner "seems to
    take a long time". The edition is accepted by the room publish; the extra copy to every listed relay (above) is
    redundancy -- and the save WAITED for it, so one slow or refusing relay held the dialog for as long as it took to
    answer. Measured here with one relay that answers after 8s: the save must be done in under 3s, and the copy to
    every listed relay must still be sent."""
    async def check(b):
        minted = await setup(b, 1280, 'Quick keeper')
        await b.js("window.__direct=[];__PC.relayPublishTo=(relays,ev)=>{__direct.push({relays,id:ev.id});"
                   "return new Promise(r=>setTimeout(()=>r({ok:true}),8000));};true")
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        res = await b.js(r'''(async()=>{const me=__PC.me().pubkey,room=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+me)||'[]').find(r=>r.name==='Quick keeper');
          const t0=performance.now();await PCConcord.saveCommunitySettings(__PC,room,{description:'quick'});
          return {ms:performance.now()-t0,direct:__direct.length}})()''')
        assert res['direct'] >= 1, ('the copy to every listed relay was dropped', res)
        assert res['ms'] < 3000, ('the save waited for a slow relay', res)
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
