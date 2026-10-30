"""A PROFILE'S TAB ROW IS POSTS / MEDIA / ARTICLES / STREAMS, AND CHIPS PICK WHAT EACH SHOWS -- ON A PHONE TOO.

"user profiles are getting congested. Can we combine Notes and Replies? For Media, good UI way to choose Videos or
Pictures only? ... we need to be UI efficient" + "make sure good on mobile too" (2026-10-30). Chosen: Posts with chips
All / Posts / Replies, Media with All / Photos / Videos / Albums. The shipped bundle at 390 and 1280 px, real clicks:
the tab row fits a phone without scrolling, Posts opens on posts only, All mixes in replies, Replies is replies only,
Photos and Videos each show one kind, Albums mounts the album view, and the chip rows fit the phone.
"has to be a good UI slider, we can't have it looking like ass": the choices are ONE segmented track whose thumb slides
under the picked choice, labels never wrap or clip.
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_profile_search_full_app import _click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

SETUP = r'''(async()=>{const me=__PC.me(), now=Math.floor(Date.now()/1000);
  const s=(c,t,tags)=>__PC.signTemplate({kind:1,pubkey:me.pubkey,created_at:now-t,tags:tags||[],content:c});
  const parent=await s('the parent post',900);
  const evs=[parent, await s('a plain post',60), await s('a photo post https://media.example/p.jpg',120),
             await s('a video post https://media.example/v.mp4',180),
             await s('a reply to myself',240,[['e',parent.id,'','root'],['p',me.pubkey]])];
  evs.forEach(e=>Store.saveEvent(e));
  const q=Relay.query.bind(Relay);
  Relay.query=async(f,...r)=>{const fl=Array.isArray(f)?f:[f];
    if(fl.some(x=>(x.authors||[]).includes(me.pubkey)&&(x.kinds||[]).includes(1))){const a=evs.slice();a.complete=true;return a;}
    return q(f,...r);};
  return me.pubkey})()'''

STATE = r'''(()=>{const L=document.querySelector('#prof-list'), row=document.querySelector('.prof-tabs'), sub=document.querySelector('#prof-sub');
  const t=n=>!!L && [...L.querySelectorAll('article, .note')].some(x=>x.textContent.includes(n));
  return {tabs:[...row.querySelectorAll('.prof-tab')].map(b=>b.dataset.tab), rowFits: row.scrollWidth<=row.clientWidth+1,
    chips:sub&&!sub.hidden?[...sub.querySelectorAll('.prof-chip')].map(c=>c.textContent+(c.classList.contains('on')?'*':'')):[],
    subFits: !sub || sub.hidden || sub.scrollWidth<=sub.clientWidth+1,
    thumbOnPick:(()=>{const th=sub&&sub.querySelector('.prof-seg-thumb'), on=sub&&sub.querySelector('.prof-chip.on');
      if(!th||!on) return false; const a=th.getBoundingClientRect(), c=on.getBoundingClientRect();
      return Math.abs((a.left+a.width/2)-(c.left+c.width/2))<4 && Math.abs(a.width-c.width)<8;})(),
    oneLine:!sub||sub.hidden||[...sub.querySelectorAll('.prof-chip')].every(c=>c.scrollWidth<=c.clientWidth+1&&c.getBoundingClientRect().height<48),
    plain:t('a plain post'), reply:t('a reply to myself'),
    imgs:L?L.querySelectorAll('.media-grid img').length:0, vids:L?L.querySelectorAll('.media-grid video').length:0,
    albums:!!document.querySelector('#prof-albums'), page:document.documentElement.scrollWidth<=innerWidth+1};})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_profile_tabs_are_groups_and_chips_pick_the_list(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 860, 'deviceScaleFactor': 1, 'mobile': width < 600})
        await desktop.login(b)
        pk = await b.js(SETUP)
        await b.js(f"__PC.openProfile('{pk}')")
        await b.until("!!document.querySelector('#prof-sub .prof-chip')")
        await asyncio.sleep(1)
        got['open'] = await b.js(STATE)

        async def chip(view):
            await b.js("document.querySelector('#prof-sub').scrollIntoView({block:'center'}); true")
            await asyncio.sleep(.2)
            await _click(b, '#prof-sub .prof-chip[data-view="%s"]' % view)
            await asyncio.sleep(.6)                                   # past the thumb's slide
            return await b.js(STATE)
        got['all'] = await chip('all')
        got['replies'] = await chip('replies')
        await _click(b, '.prof-tabs .prof-tab[data-tab="media"]')
        await asyncio.sleep(.6)
        got['media'] = await b.js(STATE)
        if os.environ.get('PC_SHOTS'):
            Path(os.environ['PC_SHOTS'], 'prof-media-%d.png' % width).write_bytes(base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
        got['photos'] = await chip('photos')
        got['videos'] = await chip('videos')
        got['albums'] = await chip('albums')
        await _click(b, '.prof-tabs .prof-tab[data-tab="notes"]')     # back to Posts: it remembers Replies
        await asyncio.sleep(.6)
        got['back'] = await b.js(STATE)
        if os.environ.get('PC_SHOTS'):
            Path(os.environ['PC_SHOTS'], 'prof-posts-%d.png' % width).write_bytes(base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    o = got['open']
    assert o['tabs'] == ['notes', 'media', 'articles', 'streams', 'search'], o
    assert o['rowFits'] and o['page'], ('the tab row or the page scrolls sideways', o)
    assert o['chips'] == ['All', 'Posts*', 'Replies'] and o['plain'] and not o['reply'], ('Posts opens on posts only', o)
    assert got['all']['plain'] and got['all']['reply'], ('All must mix posts and replies', got['all'])
    assert got['replies']['reply'] and not got['replies']['plain'], ('Replies must be replies only', got['replies'])
    m = got['media']
    assert m['chips'] == ['All*', 'Photos', 'Videos', 'Albums'] and m['subFits'], ('the media chips must fit', m)
    assert m['imgs'] >= 1 and m['vids'] >= 1, ('All media shows both kinds', m)
    assert got['photos']['imgs'] >= 1 and got['photos']['vids'] == 0, ('Photos only', got['photos'])
    assert got['videos']['vids'] >= 1 and got['videos']['imgs'] == 0, ('Videos only', got['videos'])
    assert got['albums']['albums'], ('Albums opens the album view', got['albums'])
    for k in ('open', 'all', 'replies', 'media', 'photos', 'videos', 'albums', 'back'):
        assert got[k]['thumbOnPick'] and got[k]['oneLine'], ('the slider thumb must sit under the picked choice, labels on one line', k, got[k])
    assert got['back']['chips'] == ['All', 'Posts', 'Replies*'], ('Posts remembers the chip last picked', got['back'])
