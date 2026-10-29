"""A profile is themed like a MySpace page and its song starts when it opens ("Vyram wants profiles to be themeable
like myspace", 2026-10-29) -- and none of it outlives the profile.

The shipped client in Chrome against a stub relay that answers with a signed profile (kind 0, a song in its `fields`)
and a signed theme (kind 16767, the shape Ditto and Armada publish -- plus two hostile values: a colour that is not a
colour, and a font on a host that is not a font host). The song is a real WAV served beside the page. A real mouse
click first, as a person's click on a name gives the page.
"""
import asyncio
import io
import math
import struct
import wave
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

INIT = r'''
const __SK = new Uint8Array(32).fill(2);
window.__ev=[]; for(const n of ['play','playing','pause','waiting','error','ended']) document.addEventListener(n, e=>{ if(e.target.tagName==='AUDIO') window.__ev.push(n+':'+(e.target._pcEq?1:0)); }, true);
window.__OWNER = null;
function __events(){
  const T = window.NostrTools, pk = T.getPublicKey(__SK), now = Math.floor(Date.now()/1000);
  window.__OWNER = pk;
  const k0 = T.finalizeEvent({ kind: 0, created_at: now - 60, tags: [], content: JSON.stringify({
    name: 'Myspace Tom', about: 'thanks for the add',
    fields: [['🎶 Our song', location.origin + '/song.wav']] }) }, __SK);
  const theme = T.finalizeEvent({ kind: 16767, created_at: now - 30, content: '', tags: [
    ['c', '#1c253f', 'background'], ['c', '#f6f0ea', 'text'], ['c', '#c03838', 'primary'],
    ['c', 'red;background:url(https://evil.example/x)', 'primary'],
    ['f', 'Silkscreen', 'https://cdn.jsdelivr.net/fontsource/fonts/silkscreen@latest/latin-400-normal.woff2', 'body'],
    ['f', 'Spy', 'https://tracker.example/font.woff2', 'title'],
    ['bg', 'url https://blossom.ditto.pub/ab5f.jpg', 'mode cover', 'm image/jpeg'],
    ['c', '#ff3366', 'name'], ['c', '#00ff66', 'border'], ['border', 'show'],
    ['alt', 'Active profile theme'], ['client', 'Armada']] }, __SK);
  return { pk, k0, theme };
}
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  let E=null; try{ E=__events(); }catch(_){}
  for(const f of m.slice(2)){ if(!E || !(f.authors||[]).includes(E.pk)) continue;
    if((f.kinds||[]).includes(0)) out(['EVENT',id,E.k0]);
    if((f.kinds||[]).includes(16767)) out(['EVENT',id,E.theme]); }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FS.OPEN=1;FS.CONNECTING=0;FS.CLOSED=3;window.WebSocket=FS;
'''


def _wav():
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(i / 8))) for i in range(8000 * 20)))
    return b.getvalue()


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    gen = desktop.bundle.__wrapped__()
    next(gen)
    (desktop.BUNDLE_ROOT / 'song.wav').write_bytes(_wav())
    yield
    next(gen, None)


@CHROME
def test_the_profile_is_themed_its_song_plays_and_both_stop_when_you_leave():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        # A person's click: user activation, which is what lets a page start sound.
        for t in ('mousePressed', 'mouseReleased'):
            await b.call('Input.dispatchMouseEvent', {'type': t, 'x': 700, 'y': 500, 'button': 'left', 'clickCount': 1})
        await b.until("!!window.__OWNER")
        await b.js("__PC.openProfile(window.__OWNER)")
        await b.until("document.querySelector('#feed') && document.querySelector('#feed').dataset.ptheme === '1'")
        await b.until("(()=>{const a=document.querySelector('#feed .prof-track audio');return !!a && !a.paused})()")
        await asyncio.sleep(1.5)   # the background kind-0 refresh lands here; the song must survive it
        got['eq'] = await b.js("""(()=>{const t=document.querySelector('#feed .prof-track'), i=t&&t.querySelector('.prof-eq i');
          const h1=i?i.getBoundingClientRect().height:0;
          const all=[...document.querySelectorAll('audio')].map(x=>({bound:!!x._pcEq, paused:x.paused, inFeed: !!x.closest('#feed'), connected:x.isConnected}));
          return {all, events: window.__ev||[], playingClass: !!t && t.classList.contains('playing'), anim: i?getComputedStyle(i).animationName:'', running: i?i.getAnimations().length:0,
                  body: document.body.className, h1};})()""")
        got['on'] = await b.js("""(()=>{const f=document.querySelector('#feed'), cs=getComputedStyle(f), a=document.querySelector('#feed .prof-track audio');
          return {bg: f.style.getPropertyValue('--bg'), text: f.style.getPropertyValue('--text'), accent: f.style.getPropertyValue('--accent'),
                  img: f.style.getPropertyValue('--pt-img'), computedBg: cs.backgroundColor,
                  playing: !!a && !a.paused, chip: (document.querySelector('.pt-song')||{}).textContent || '',
                  styleText: f.getAttribute('style') || ''};})()""")
        got['fonts'] = await b.js("[...document.fonts].map(f=>f.family)")
        # Vyram's marked-up profile: the name's own colour, and a border (colour + on/off) on the banner and avatar.
        got['parts'] = await b.js("""(()=>{const h=document.querySelector('.prof .pbody h2'), bn=document.querySelector('.prof .banner'), av=document.querySelector('.prof .pav');
          const c=e=>e?getComputedStyle(e):{};
          return {name: c(h).color, bannerBorder: c(bn).borderTopColor, bannerWidth: c(bn).borderTopWidth, avatarBorder: c(av).borderTopColor};})()""")
        await b.js("window.__song=document.querySelector('#feed .prof-track audio'); __PC.switchView('global')")
        await b.until("!document.querySelector('#feed .prof')")
        await asyncio.sleep(0.5)
        got['off'] = await b.js("""(()=>{const f=document.querySelector('#feed');
          return {themed: f.dataset.ptheme || '', bg: f.style.getPropertyValue('--bg'), img: f.style.getPropertyValue('--pt-img'),
                  songPaused: !!window.__song && window.__song.paused};})()""")
        got['errors'] = await b.js("window.__errors || []")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    on, off = got['on'], got['off']
    assert on['bg'] == '#1c253f' and on['text'] == '#f6f0ea' and on['accent'] == '#c03838', on
    assert on['computedBg'] == 'rgb(28, 37, 63)', ("the profile does not show the theme's background", on)
    assert on['img'] == 'url("https://blossom.ditto.pub/ab5f.jpg")', on
    assert 'evil.example' not in on['styleText'], ("a value that is not a colour reached the page's CSS", on)
    assert not any('Spy' in f for f in got['fonts']), ("a font from a host that is not a font host was loaded", got['fonts'])
    parts = got['parts']
    assert parts['name'] == 'rgb(255, 51, 102)', ("the name does not take its own colour", parts)
    assert parts['bannerBorder'] == 'rgb(0, 255, 102)' and float(parts['bannerWidth'][:-2]) > 1, ("the banner border is not the theme's", parts)   # 2px x the page's zoom
    assert parts['avatarBorder'] == 'rgb(0, 255, 102)', parts
    eq = got['eq']
    assert eq['playingClass'] and eq['anim'] == 'profEq' and eq['running'] > 0, ("the equaliser bars do not move while the song plays", eq)
    assert on['playing'] and 'Our song' in on['chip'], ("the profile's song is not playing on open", on)
    assert off == {'themed': '', 'bg': '', 'img': '', 'songPaused': True}, ("the theme or the song outlived the profile", off)
