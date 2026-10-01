"""Texts opens a long conversation on its newest 20 messages and reaches back 40 at a time.

"Text Performance is very slow. Should we paginate it at the latest 20 messages? per sender I mean."
paintThread drew EVERY message of a conversation -- years of them -- and redrew them all on every
receipt, focus change and incoming text, and every picture placeholder in it was fetched and
decrypted. Driven in the real bundle at phone and desktop width:

  * 400 messages open as the newest 20, at the bottom, with a "Show older" control;
  * scrolling to the top draws 40 more ABOVE, and the message that was at the top stays put;
  * only the drawn messages' pictures are fetched -- and each for the RIGHT message: a tapback earlier
    in the page used to shift the index, drawing a picture from the wrong message (or none);
  * a new text arriving shows at the bottom; opening the conversation again starts at the newest page.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now(), A='+15550100';
  S.msgs.clear();
  for(let i=0;i<400;i++){
    const m={doc:'d'+i, address:A, body:'message '+i, date:now-(400-i)*60000, incoming:i%2===0, parts:[], _at:1};
    if(i>=395) m.parts=[{sha:('s'+i).padEnd(64,'0'), ct:'image/gif', name:'p'+i+'.gif'}];
    if(i===392){ m.body='see you'; m.incoming=true; }
    if(i===393){ m.body='Loved “see you”'; m.incoming=false; }      // a tapback: drawn as a chip
    S.msgs.set(m.doc, m);
  }
  S.ready=true; S.open=''; S.q='';
  window.__fetched=[];
  const gif='data:image/gif;base64,R0lGODlhAQABAAAAACw=';
  __PC.encFileUrl=async sha=>{ __fetched.push(sha); return gif; };
  PCSms.refreshNames();
})()
"""

STATE = r"""(()=>{const l=document.querySelector('.sms-msgs'); const b=[...l.querySelectorAll('.bubble[data-doc]')];
  return {n:b.length, first:b[0]&&b[0].dataset.doc, last:b[b.length-1]&&b[b.length-1].dataset.doc,
          older:!!document.querySelector('#sms-older'), bottom:l.scrollHeight-l.scrollTop-l.clientHeight<80,
          fits:l.scrollWidth<=l.clientWidth+1}})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,height,mobile', [(390, 844, True), (1280, 900, False)])
def test_a_long_conversation_opens_on_its_newest_page_and_reaches_back(width, height, mobile):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': mobile})
        await open_texts(b)
        await b.js(SEED)
        await b.js("document.querySelector('#sms-back') && document.querySelector('#sms-back').click()")
        await b.until("[...document.querySelectorAll('.sms-thread')].length>=1")
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("document.querySelectorAll('.sms-msgs .bubble[data-doc]').length>0")
        await asyncio.sleep(.3)
        st = await b.js(STATE)
        # 400 messages, one of them a tapback drawn as a chip: the newest 20 DRAWN ones.
        assert st['n'] == 20 and st['last'] == 'd399' and st['older'], st
        assert st['bottom'] and st['fits'], st

        # Only the drawn pictures, each fetched for its own message (d395..d399).
        await b.until("__fetched.length>=5")
        await asyncio.sleep(.3)
        got = sorted(set(await b.js("__fetched")))
        assert got == sorted(('s%d' % i).ljust(64, '0') for i in range(395, 400)), got

        # Scroll to the top: 40 more above, and the message that was at the top stays where it was.
        # Read where the top message sits AT the top, in the same turn as the scroll -- before the
        # scroll event runs and draws the older page above it.
        top_before = await b.js("(()=>{const l=document.querySelector('.sms-msgs');l.scrollTop=0;const e=l.querySelector('.bubble[data-doc]');return {doc:e.dataset.doc,y:e.getBoundingClientRect().top}})()")
        await b.until("document.querySelectorAll('.sms-msgs .bubble[data-doc]').length===60")
        after = await b.js("(()=>{const e=document.querySelector('.sms-msgs .bubble[data-doc=\"'+%r+'\"]');return e.getBoundingClientRect().top})()" % top_before['doc'])
        assert abs(after - top_before['y']) <= 2, (top_before, after)

        # "Show older" reaches back too.
        await b.js("document.querySelector('#sms-older').click()")
        await b.until("document.querySelectorAll('.sms-msgs .bubble[data-doc]').length===100")

        # A new text arrives: it is drawn at the bottom and nothing above it is lost.
        await b.js("(()=>{const S=PCSms._state();S.msgs.set('dnew',{doc:'dnew',address:'+15550100',body:'just now',date:Date.now(),incoming:true,parts:[],_at:1});PCSms.refreshNames();})()")
        await b.until("(()=>{const b=[...document.querySelectorAll('.sms-msgs .bubble[data-doc]')];return b.length===101&&b[b.length-1].dataset.doc==='dnew'})()")

        # Leaving and opening it again starts at the newest page.
        await b.js("document.querySelector('#sms-back').click()")
        await b.until("!!document.querySelector('.sms-thread')")
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("document.querySelectorAll('.sms-msgs .bubble[data-doc]').length===20")
    asyncio.run(desktop.with_browser('online', '', check))
