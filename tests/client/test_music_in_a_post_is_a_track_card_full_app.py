"""A MUSIC FILE IN A POST LOOKS LIKE THE PLAYER, NOT A FORM CONTROL.

"if a person puts a music file in a post, make it look cool like the desktop widget/profile page". A
posted .mp3/.ogg/.flac link was a bare <audio controls> after a line break. Now it is the profile's track
card: the ♪ mark, the file's readable name, and the cyan→magenta equaliser that moves only while the
track plays. The real client, the real feed renderer (via Bookmarks), at phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
(()=>{let built=null;Object.defineProperty(window,'__events',{configurable:true,set(v){built=v;},get(){
  if(built||!window.NostrTools)return built||[];
  const me=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(7);
  const note=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-60,
    content:'new track up https://media.example/music/Night_City_Drive.mp3 enjoy',tags:[]},other);
  const list=NostrTools.finalizeEvent({kind:10003,created_at:Math.floor(Date.now()/1000)-30,content:'',tags:[['e',note.id]]},me);
  return built=[note,list];}});})();
'''

CARD = r"""(()=>{const c=document.querySelector('#feed .note-track');if(!c)return null;const r=c.getBoundingClientRect();
  const a=c.querySelector('audio');
  return {name:(c.querySelector('.prof-track-name')||{}).textContent||'',bars:c.querySelectorAll('.prof-eq i').length,
          mark:!!c.querySelector('.prof-track-mark'),audio:!!a&&a.hasAttribute('controls'),
          fits:r.left>=-1&&r.right<=innerWidth+1&&r.width>120,bare:document.querySelectorAll('#feed .note audio:not(.prof-track audio)').length,
          playing:c.classList.contains('playing')};})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_posted_music_file_is_a_track_card(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchView('bookmarks')")
        await b.until("!!document.querySelector('#feed .note-track')")
        got['rest'] = await b.js(CARD)
        await b.js("document.querySelector('#feed .note-track audio').dispatchEvent(new Event('play'))")
        got['on'] = await b.js(CARD)
        await b.js("document.querySelector('#feed .note-track audio').dispatchEvent(new Event('pause'))")
        got['off'] = await b.js(CARD)
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, SEED))
    r = got['rest']
    assert r, "the posted music file is not a track card"
    assert r['name'] == 'Night City Drive' and r['mark'] and r['bars'] == 12 and r['audio'], r
    assert r['fits'] and r['bare'] == 0, r
    assert r['playing'] is False and got['on']['playing'] is True and got['off']['playing'] is False, got
    assert not got['errors'], got['errors']
