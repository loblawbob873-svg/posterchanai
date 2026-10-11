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
