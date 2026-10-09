"""A MUSIC FILE IN A POST SHOWS AS A TRACK — ITS COVER, TITLE AND ARTIST — LIKE DITTO AND ARMADA.

"if a person puts a music file in a post, make it look cool like the desktop widget/profile page", then
"try to get it to show the audio metadata like ditto/amethyst does it" with a post comparing Armada and
Ditto (nevent1qqsqqqqqlxtf3…): cover art, the file's own title, "artist · album", a round play button,
bars that fill as it plays, and the time. That post also points out that Ditto's Blossom serves an MP3
as .mpga (audio/mpeg's registered extension) — which PosterChan did not recognise as audio at all.
The real client and the real feed renderer (via Bookmarks); the media server is a fixture serving REAL
ID3v2.3 bytes; the audio element's playback is stubbed (headless Chrome has no file to decode).
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_audio_tags import id3v23


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__tagBytes=Uint8Array.from(atob('B64'),c=>c.charCodeAt(0));window.__ranges=[];
{const up=window.fetch;window.fetch=(url,opts)=>{const u=String(url);
  if(u.startsWith('https://media.example/')){__ranges.push((opts&&opts.headers&&opts.headers.Range)||'');
    return Promise.resolve(new Response(__tagBytes,{status:206,headers:{'Content-Type':'audio/mpeg'}}));}
  return up(url,opts);};}
// Playback: no real file to decode in headless Chrome, so play/pause/duration behave like a 200 s track.
Object.defineProperty(HTMLMediaElement.prototype,'duration',{configurable:true,get(){return 200;}});
{let t=new WeakMap();Object.defineProperty(HTMLMediaElement.prototype,'currentTime',{configurable:true,
  get(){return t.get(this)||0;},set(v){t.set(this,v);this.dispatchEvent(new Event('timeupdate'));}});}
{const paused=new WeakMap();Object.defineProperty(HTMLMediaElement.prototype,'paused',{configurable:true,get(){return paused.get(this)!==false;}});
 HTMLMediaElement.prototype.play=function(){paused.set(this,false);this.dispatchEvent(new Event('play'));return Promise.resolve();};
 HTMLMediaElement.prototype.pause=function(){paused.set(this,true);this.dispatchEvent(new Event('pause'));};}
(()=>{let built=null;Object.defineProperty(window,'__events',{configurable:true,set(v){built=v;},get(){
  if(built||!window.NostrTools)return built||[];
  const me=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(7), now=Math.floor(Date.now()/1000);
  const a=NostrTools.finalizeEvent({kind:1,created_at:now-60,content:'new track https://media.example/music/Playground.mp3',tags:[]},other);
  const b=NostrTools.finalizeEvent({kind:1,created_at:now-50,content:'from ditto https://media.example/50b4aa.mpga',tags:[]},other);
  const list=NostrTools.finalizeEvent({kind:10003,created_at:now-30,content:'',tags:[['e',a.id],['e',b.id]]},me);
  return built=[a,b,list];}});})();
'''.replace('B64', base64.b64encode(id3v23()).decode())

LAYOUT = r"""[...document.querySelectorAll('#feed .pc-track')].map(c=>{const r=c.getBoundingClientRect();
  const inside=sel=>{const e=c.querySelector(sel),q=e&&e.getBoundingClientRect();return !!q&&q.width>0&&q.left>=r.left-1&&q.right<=r.right+1&&q.top>=r.top-1&&q.bottom<=r.bottom+1;};
  const play=c.querySelector('.pct-play').getBoundingClientRect(),t=c.querySelector('.pct-title'),sub=c.querySelector('.pct-sub');
  return {art:inside('.pct-art'),play:inside('.pct-play'),wave:inside('.pct-wave'),time:inside('.pct-time'),
    tap:Math.min(play.width,play.height),titleClipped:t.scrollWidth<=t.clientWidth+1||getComputedStyle(t).textOverflow==='ellipsis',
    subOneLine:sub.getBoundingClientRect().height<=parseFloat(getComputedStyle(sub).lineHeight||'20')*1.6+2,
    waveW:c.querySelector('.pct-wave').getBoundingClientRect().width,overflow:document.documentElement.scrollWidth>innerWidth+1};})"""

CARDS = r"""[...document.querySelectorAll('#feed .pc-track')].map(c=>{const r=c.getBoundingClientRect(),img=c.querySelector('.pct-art img');
  return {src:c.dataset.src,title:c.querySelector('.pct-title').textContent,sub:c.querySelector('.pct-sub').textContent,
    cover:!!img&&img.naturalWidth>=0&&/^blob:/.test(img.src),fits:r.left>=-1&&r.right<=innerWidth+1,
    playing:c.classList.contains('playing'),label:c.querySelector('.pct-play').getAttribute('aria-label'),
    lit:c.querySelectorAll('.pct-wave i.on').length,bars:c.querySelectorAll('.pct-wave i').length,
    time:c.querySelector('.pct-time').textContent,now:c.querySelector('.pct-wave').getAttribute('aria-valuenow')};})"""


async def _click(b, sel, fx=0.5):
    r = await b.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});e.scrollIntoView({{block:'center'}});const r=e.getBoundingClientRect();return [r.left+r.width*{fx},r.top+r.height/2];}})()")
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', dict(type=kind, x=r[0], y=r[1], button='left', clickCount=1))
    await asyncio.sleep(.2)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [360, 390, 1280])
def test_a_posted_track_shows_its_cover_title_and_artist_and_plays(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchView('bookmarks')")
        await b.until("document.querySelectorAll('#feed .pc-track').length===2")
        await b.until("[...document.querySelectorAll('#feed .pc-track .pct-title')].every(t=>t.textContent==='Playground')")
        got['rest'] = await b.js(CARDS)
        got['layout'] = await b.js(LAYOUT)
        import os
        if os.environ.get('PC_TRACK_SHOTS'):
            await b.js("document.querySelector('#feed .pc-track').scrollIntoView({block:'center'})")
            shot = (await b.call('Page.captureScreenshot', {'format': 'png'}))['data']
            Path(os.environ['PC_TRACK_SHOTS'], f'track-{width}.png').write_bytes(base64.b64decode(shot))
        got['viewUrl'] = await b.js("location.href")
        await _click(b, '#feed .pc-track .pct-play')
        got['playing'] = await b.js(CARDS)
        await _click(b, '#feed .pc-track .pct-wave', 0.5)
        got['seeked'] = await b.js(CARDS)
        got['stillHere'] = await b.js("location.href")
        await _click(b, '#feed .pc-track:nth-of-type(1) .pct-play')
        got['errors'] = await b.js('__errors')
        got['ranges'] = await b.js('__ranges')

    asyncio.run(desktop.with_browser('online', '', check, SEED))
    rest = got['rest']
    assert len(rest) == 2, ("the .mp3 and the .mpga must both be tracks", rest)
    for c in rest:
        assert c['title'] == 'Playground' and c['sub'] == 'Bea Miller · Arcane: League of Legends', c
        assert c['cover'] and c['fits'] and not c['playing'] and c['bars'] == 40, c
    for L in got['layout']:
        assert L['art'] and L['play'] and L['wave'] and L['time'], ("part of the track sticks out of its card", width, L)
        assert L['tap'] >= 40, ("the play button is too small to tap", width, L)
        assert L['titleClipped'] and L['subOneLine'] and L['waveW'] >= 80 and not L['overflow'], (width, L)
    assert all(r.startswith('bytes=0-') for r in got['ranges']), ("tags must be read from the head of the file only", got['ranges'])
    first = got['playing'][0]
    assert first['playing'] and first['label'].startswith('Pause'), ("play did not play", first)
    s = got['seeked'][0]
    assert s['now'] in ('49', '50', '51') and 18 <= s['lit'] <= 22 and s['time'] == '1:40', ("seeking by the bars failed", s)
    assert got['stillHere'] == got['viewUrl'], "pressing the player opened the post instead"
    assert not got['errors'], got['errors']
