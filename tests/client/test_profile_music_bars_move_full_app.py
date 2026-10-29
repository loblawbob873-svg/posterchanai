"""The profile song's equaliser MOVES while the song plays ("the effects do not work on music files on profile. no bar
movement" ... "this is a regression we need test case for", 2026-10-29).

What froze it, from 2026-10-09: app.js's page-wide media listener (written for the post track cards) also toggled a
profile card's `.playing` -- to false on every event that was not play/playing, and `timeupdate` fires several times
a second while audio plays. The shipped client in Chrome, a profile with a real WAV in its `fields`, the song started
after a real click, and the bars read from the running animations after the profile's background refresh.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
import io
import math
import struct
import wave

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
# A stub relay that answers with one signed profile whose `fields` carry a song served beside the page.
INIT = r'''
const __SK = new Uint8Array(32).fill(2);
window.__OWNER = null;
function __events(){
  const T = window.NostrTools, pk = T.getPublicKey(__SK), now = Math.floor(Date.now()/1000);
  window.__OWNER = pk;
  const k0 = T.finalizeEvent({ kind: 0, created_at: now - 60, tags: [], content: JSON.stringify({
    name: 'Myspace Tom', fields: [['🎶 Our song', location.origin + '/song.wav']] }) }, __SK);
  return { pk, k0 };
}
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  let E=null; try{ E=__events(); }catch(_){}
  for(const f of m.slice(2)) if(E && (f.authors||[]).includes(E.pk) && (f.kinds||[]).includes(0)) out(['EVENT',id,E.k0]);
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
def test_the_bars_move_while_the_profile_song_plays_and_stop_when_it_stops():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        for t in ('mousePressed', 'mouseReleased'):
            await b.call('Input.dispatchMouseEvent', {'type': t, 'x': 700, 'y': 500, 'button': 'left', 'clickCount': 1})
        await b.until("!!window.__OWNER")
        await b.js("__PC.openProfile(window.__OWNER)")
        await b.until("!!document.querySelector('#feed .prof-track audio')")
        await b.js("document.querySelector('#feed .prof-track audio').play()")
        await b.until("(()=>{const a=document.querySelector('#feed .prof-track audio');return !a.paused && a.currentTime>0.5})()")
        await asyncio.sleep(1.5)   # timeupdate has fired many times by now, and the kind-0 refresh has landed
        bars = """(()=>{const t=document.querySelector('#feed .prof-track'), i=t.querySelector('.prof-eq i');
          return {playing: t.classList.contains('playing'), running: i.getAnimations().length, paused: t.querySelector('audio').paused};})()"""
        got['on'] = await b.js(bars)
        await b.js("document.querySelector('#feed .prof-track audio').pause()")
        await asyncio.sleep(0.3)
        got['off'] = await b.js(bars)

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['on'] == {'playing': True, 'running': got['on']['running'], 'paused': False} and got['on']['running'] > 0, \
        ("the bars do not move while the song plays", got['on'])
    assert got['off']['playing'] is False and got['off']['running'] == 0, ("the bars keep dancing over silence", got['off'])
