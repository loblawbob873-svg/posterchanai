"""Community settings → Banner: the two buttons no test pressed (2026-10-22: every button gets a test).

  * '#cc-banner-pick' ("Upload banner…" / "Change banner…") opens the file chooser of the banner input (and only
    that one: image types), and the file chosen there is what gets uploaded and published.
  * '#cc-banner-remove' is offered only over a community that HAS a banner; pressing it stages the removal (said in
    the status line, preview emptied) and changes nothing until Save; Save publishes CORD-02 metadata with no
    banner and the banner leaves the channel list.
Real minted community, real reader, real signer, real DOM; the relay and blob server are in-page fixtures.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import CONTROLS, OPEN_ROOM
from tests.client.test_concord_banner_full_app import BANNER_SHOWN, setup
from tests.client.test_concord_channel_controls_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


EXTRA = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"

# The native file chooser cannot be driven headless; record which input was asked to open it.
CHOOSER = r'''(()=>{window.__chooser=[];const c=HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click=function(){if(this.type==='file'){__chooser.push({id:this.id,accept:this.accept});return;}return c.call(this);};
  __PC.uploadBlob=async(blob)=>{const bytes=new Uint8Array(await blob.arrayBuffer());
    const url='https://blobs.invalid/pick-'+(__uploads.push(bytes.length));__blobs.set(url,bytes);return url;};})()'''

BANNER_POINTER = "PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").banner"


async def open_settings(b):
    await b.until("!!document.querySelector('#cc-edit-icon')")
    await click(b, '#cc-edit-icon')
    await b.until("!document.querySelector('#cc-settings-dialog').classList.contains('hidden')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_upload_banner_opens_the_banner_file_chooser_and_publishes_the_choice():
    async def check(b):
        minted = await setup(b, 1280, 'Banner picker')
        await b.js(CHOOSER)
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await open_settings(b)
        assert await b.js("document.querySelector('#cc-banner-pick').textContent.trim()") == 'Upload banner…'
        assert not await b.js("!!document.querySelector('#cc-banner-remove')"), 'Remove offered with no banner to remove'
        await click(b, '#cc-banner-pick')
        await b.until("__chooser.length===1")
        assert await b.js("__chooser[0]") == {"id": "cc-banner-file", "accept": "image/png,image/jpeg,image/gif,image/webp"}
        # The chooser answers with a picture: it is uploaded and, on Save, published as the banner.
        await b.js(r'''(async()=>{const bytes=await __pngBytes(1200,300,'#3c6'),dt=new DataTransfer();
          dt.items.add(new File([bytes],'banner.png',{type:'image/png'}));const f=document.querySelector('#cc-banner-file');
          f.files=dt.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()''')
        await b.until("/ready|uploaded/i.test(document.querySelector('#cc-banner-status')?.textContent||'')")
        await click(b, '#cc-settings-save')
        await b.until(BANNER_SHOWN)
        assert (await b.js(BANNER_POINTER))['url'] == 'https://blobs.invalid/pick-' + str(await b.js("__uploads.length"))
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_remove_banner_stages_the_removal_and_save_publishes_it():
    async def check(b):
        minted = await setup(b, 1280, 'Banner remover')
        await b.js(CHOOSER)
        await b.js(r'''(async()=>{const pointer=await __sealPointer(await __pngBytes(900,240,'#c33'));
          const made=await PosterCordReader.createMetadataWrap(__cordBundle,''' + CONTROLS + r''',{bannerRef:pointer,description:'kept'},__PC.me().pubkey,__PC.signTemplate);
          __relaySeed([made.wrap]);})()''')
        await b.until("!!(" + BANNER_POINTER + ")")
        room = minted['room']
        room['cord']['hydrated'] = False
        await b.js(OPEN_ROOM + '(' + json.dumps(room) + ')')
        await b.until(BANNER_SHOWN)
        await open_settings(b)
        await b.until("!!document.querySelector('#cc-banner-remove')")
        assert await b.js("document.querySelector('#cc-banner-pick').textContent.trim()") == 'Change banner…'
        await click(b, '#cc-banner-remove')
        await b.until("document.querySelector('#cc-banner-status').textContent==='The banner will be removed when you save.'")
        assert await b.js("document.querySelector('#cc-banner-preview').classList.contains('empty')&&!document.querySelector('#cc-banner-preview img')")
        # Staged, not done: nothing published yet, the community still has its banner.
        await asyncio.sleep(.4)
        assert await b.js("!!(" + BANNER_POINTER + ")"), 'Remove published before Save'
        assert await b.js(BANNER_SHOWN)
        await click(b, '#cc-settings-save')
        await b.until("!(" + BANNER_POINTER + ")")
        await b.until("!document.querySelector('.cc-channels .cc-banner img')")
        assert await b.js("PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").description") == 'kept'
        assert await b.js("__chooser.length") == 0 and await b.js("__uploads.length") == 0
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
