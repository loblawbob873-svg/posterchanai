"""ATTACHING A FILE IN A CONCORD ROOM DID NOTHING, AND NOTHING SAID SO.

Reported as "attaching files from blossom in concord room did nothing, wtf! this is basic testing!"
-- and it was right about the testing: this repo has ninety-odd Concord tests and not one of them
had ever pressed the paperclip. `check_concord_mobile.py` stubs `blossomPicker:null, modal:null`,
so the attach path was excluded by construction.

THE BUG. `insertBlossomAttachment` (and the device-upload path beside it) closed over the `#cc-input`
element that `bind()` happened to see. `render()` rewrites the whole workspace, so after any repaint
that element is DETACHED -- and this screen repaints constantly: a relay message, a profile, a room
icon, a delivery status update. `input.value += url` then succeeds on a node nobody can see. No
error, no toast, nothing in the console.

Browsing a drive takes seconds, so a repaint inside that window is the ORDINARY case in a live room,
which is why it worked in every quiet fixture and never for the user. MEASURED both ways against the
real bundled app before a line was changed: pick with no repaint in between and the URL lands; force
one repaint and it is silently lost.

AND THE SECOND BUG HID THE FIRST: blossomPicker ran the caller's `onPick` inside `catch(_){}`, so a
callback that threw was indistinguishable from a file nobody picked. It reports now.
"""
import asyncio
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The windowed desktop puts Concord in its own frame; this bug is about the composer, not the shell.
EXTRA = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({"
         "...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

ROOM = r'''(()=>{
  const room={name:'Attach fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
    channels:[{id:'fixture-general',name:'general'}],
    cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.invites',JSON.stringify([room]));
  localStorage.setItem('pc.concord.active','0');
  window.__concordTags=null;
  window.PosterCordReader={
    inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]}),
    createChatWrap:async(_b,_w,_ch,_text,_owner,sign,tags)=>{
      window.__concordTags=JSON.parse(JSON.stringify(tags||[]));
      const sealed=await sign({kind:20013,created_at:Math.floor(Date.now()/1000),content:'fixture',tags:[]});
      return {rumorId:sealed.id,wrap:{...sealed,kind:1059},ms:Date.now()};
    }};
  __PC.switchMessagesTab('concord');
})()'''

# One file on the drive, served without a network.
LISTING = r'''(()=>{const real=window.fetch;
  window.fetch=(u,o)=>String(u).includes('/list/')
    ? Promise.resolve(new Response(JSON.stringify([
        {url:'https://files.test/abc.png',sha256:'a'.repeat(64),size:1234,type:'image/png',uploaded:1700000000}]),
        {status:200,headers:{'content-type':'application/json'}}))
    : real(u,o);
  return true;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('repaint', [True, False])
def test_a_file_picked_from_the_drive_reaches_the_composer(repaint):
    """The repaint case is the one that was broken; the quiet case is kept so a fix that breaks the
    ordinary path cannot pass."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride",
                     {"width": 1280, "height": 850, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input')")
        await b.js(LISTING)
        await b.js("document.querySelector('#cc-attach').click()")
        await b.until("!!document.querySelector('#cc-attach-blossom')")
        await b.js("document.querySelector('#cc-attach-blossom').click()")
        await b.until("!!document.querySelector('.bp-pick-card')")
        if repaint:
            # Exactly what a relay message, a profile or an icon load does while you browse.
            await b.js("PCConcord.render()")
        await b.js("document.querySelector('.bp-pick-card').click()")
        await b.until("(()=>{const el=document.querySelector('#cc-input');"
                      "return !!el && el.value.includes('files.test/abc.png');})()")

        # …and it must survive the SEND, or the picker only ever decorated a text box.
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!window.__concordTags")
        tags = await b.js("JSON.stringify(window.__concordTags)")
        assert 'imeta' in tags and 'files.test/abc.png' in tags, tags
        assert 'm image/png' in tags, 'the attachment lost its mime type: ' + tags

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_picker_says_so_when_the_caller_cannot_take_the_file():
    """`catch(_){}` made a throwing callback identical to a file nobody picked, which is what kept
    the bug above invisible for as long as it existed."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride",
                     {"width": 1280, "height": 850, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js(LISTING)
        # Read the toast off the SCREEN. `blossomPicker` calls the module's own `toast`, not the
        # one on the bridge, so wrapping the bridge measures a seam the picker never crosses.
        await b.js("__PC.blossomPicker(null,()=>{throw new Error('fixture refuses');})")
        await b.until("!!document.querySelector('.bp-pick-card')")
        await b.js("document.querySelector('.bp-pick-card').click()")
        await b.until("document.body.innerText.indexOf('could not attach')>=0")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
