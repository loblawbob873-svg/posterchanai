"""A QUOTED NOTE INSIDE A POST ON A PROFILE SITS TIGHT -- NO BLANK BANDS ABOVE ITS AUTHOR OR ITS TEXT.

Vyram, 'Quote of Note Gap' (15880b08): "Quoting a note creates a gap ... That huge space shouldn't be there" -- on a
PROFILE ("It appears when your viewing someones notes on their profile"). Measured live on his test profile: the card
inherited the post text's `white-space: pre-wrap`, so the newlines and indentation of its own markup drew as 55 px above
the author line and 71 px above the text. The shipped bundle, a post quoting another, on the author's profile.
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

SETUP = r'''(async()=>{const me=__PC.me(), now=Math.floor(Date.now()/1000);
  const quoted=await __PC.signTemplate({kind:1,pubkey:me.pubkey,created_at:now-600,tags:[],content:'My people someones looking to impersonate making multiple npubs.'});
  const nev=NostrTools.nip19.neventEncode({id:quoted.id, author:me.pubkey});
  const quoting=await __PC.signTemplate({kind:1,pubkey:me.pubkey,created_at:now-60,tags:[['e',quoted.id,'','mention']],content:"There's a possibility that team soapbox may be behind this.\n\nnostr:"+nev});
  [quoted,quoting].forEach(e=>Store.saveEvent(e));
  const meta=await __PC.signTemplate({kind:0,pubkey:me.pubkey,created_at:now-900,tags:[],content:JSON.stringify({name:'Vitor',nip05:'user15545a8a@poster.place'})});
  Store.saveProfile(meta);
  const q=Relay.query.bind(Relay);
  Relay.query=async(f,...r)=>{const fl=Array.isArray(f)?f:[f];
    if(fl.some(x=>(x.ids||[]).includes(quoted.id))){const a=[quoted];a.complete=true;return a;}
    if(fl.some(x=>(x.authors||[]).includes(me.pubkey)&&(x.kinds||[]).includes(1))){const a=[quoting,quoted];a.complete=true;return a;}
    return q(f,...r);};
  return me.pubkey})()'''

GAPS = r'''(()=>{const n=[...document.querySelectorAll('#prof-list article, #prof-list .note')].find(x=>/team soapbox/.test(x.textContent));
  const q=n&&n.querySelector('.quoted'); if(!q) return null;
  const r=e=>e.getBoundingClientRect(), qr=r(q), hd=q.querySelector('.hd'), tx=q.querySelector('.txt');
  const h=q.querySelector('.handle'), t=q.querySelector('.time'), nm=q.querySelector('.hd .name');
  return {rows:h&&t&&nm?{nameTop:Math.round(r(nm).top),handleTop:Math.round(r(h).top),timeTop:Math.round(r(t).top),
            handleFull:h.scrollWidth<=h.clientWidth+1,handle:h.textContent,timeRight:Math.round(qr.right-r(t).right)}:null,
    top:Math.round(r(hd).top-qr.top), toText:tx?Math.round(r(tx).top-r(q.querySelector('.qsub')||hd).bottom):null, ws:getComputedStyle(q).whiteSpace, parent:q.parentElement.className, lead:JSON.stringify(q.innerHTML.slice(0,30))};})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [412, 1280])
def test_a_quoted_note_on_a_profile_has_no_blank_bands(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 600})
        await desktop.login(b)
        pk = await b.js(SETUP)
        await b.js(f"__PC.openProfile('{pk}')")
        for _ in range(60):
            g = await b.js(GAPS)
            if g and g['toText'] is not None:
                break
            await asyncio.sleep(.25)
        await asyncio.sleep(.5)
        got['g'] = await b.js(GAPS)
        if os.environ.get('PC_SHOTS'):
            await b.js("document.querySelector('#prof-list .quoted').scrollIntoView({block:'center'}); 1")
            Path(os.environ['PC_SHOTS'], 'quote-%d.png' % width).write_bytes(base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    g = got['g']
    assert g, 'the quoted card was not drawn on the profile'
    assert g['top'] <= 16 and g['toText'] <= 16, ('blank bands inside the quoted card', g)
    # Vyram's follow-up: a short name left the time alone on a row of its own, and the address was cut while there was
    # room. One shape now: the name, then the address and the time sharing the next row, the address in full.
    rows = g['rows']
    assert rows, ('the quoted card has no address row', g)
    assert rows['handleTop'] >= rows['nameTop'] + 8 and abs(rows['handleTop'] - rows['timeTop']) <= 4, ('address and time must share the row under the name', rows)
    assert rows['handleFull'] and rows['handle'] == 'user15545a8a@poster.place', ('the address was cut although it fits', rows)
