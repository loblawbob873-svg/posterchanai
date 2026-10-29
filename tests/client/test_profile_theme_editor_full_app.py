"""Making a MySpace page: the Theme button on your own profile, and the viewer's two switches.

The shipped client in Chrome against a stub relay that records what it is sent and accepts it: press Theme, pick a
preset, give the name its own colour and the borders none (Vyram's marked-up profile, 2026-10-29), Save -- the event
that goes out is Ditto's kind 16767 with the three shared colours first, and the page repaints at once. Then, with
Settings -> Appearance's switches off, a themed profile with a song shows neither.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

INIT = r'''
window.__published = [];
const __SK2 = new Uint8Array(32).fill(2);
function __other(){
  const T = window.NostrTools, pk = T.getPublicKey(__SK2), now = Math.floor(Date.now()/1000);
  window.__OTHER = pk;
  return { pk,
    k0: T.finalizeEvent({ kind: 0, created_at: now - 60, tags: [], content: JSON.stringify({ name: 'Themed', fields: [['🎶 Song', location.origin + '/x.wav']] }) }, __SK2),
    theme: T.finalizeEvent({ kind: 16767, created_at: now - 30, content: '', tags: [['c','#1c253f','background'],['c','#f6f0ea','text'],['c','#c03838','primary']] }, __SK2) };
}
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  if(m[0]==='EVENT'){ window.__published.push(m[1]); out(['OK', m[1].id, true, '']); return; }
  if(m[0]!=='REQ') return; const id=m[1];
  let O=null; try{ O=__other(); }catch(_){}
  for(const f of m.slice(2)){
    if(O && (f.authors||[]).includes(O.pk)){ if((f.kinds||[]).includes(0)) out(['EVENT',id,O.k0]); if((f.kinds||[]).includes(16767)) out(['EVENT',id,O.theme]); }
    for(const e of window.__published) if((f.kinds||[]).includes(e.kind) && (f.authors||[]).includes(e.pubkey)) out(['EVENT',id,e]);
  }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FS.OPEN=1;FS.CONNECTING=0;FS.CLOSED=3;window.WebSocket=FS;
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@CHROME
def test_the_theme_button_publishes_a_ditto_theme_with_vyrams_parts_and_the_page_repaints():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#theme-prof')")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('.pt-ed [data-preset]')")
        await b.js("""(()=>{const ed=document.querySelector('.pt-ed');
          ed.querySelector('[data-preset="Paper"]').click();
          const row=ed.querySelector('.pt-row[data-k="p:name"]'), inp=row.querySelector('input');
          inp.value='#b4472f'; inp.dispatchEvent(new Event('input',{bubbles:true}));
          const bd=ed.querySelector('#pt-border'); bd.value='none'; bd.dispatchEvent(new Event('change',{bubbles:true}));
          window.__prevBg=getComputedStyle(ed.querySelector('#pt-prev')).backgroundColor;
          ed.querySelector('#pt-save').click();})()""")
        await b.until("window.__published.some(e=>e.kind===16767)")
        await b.until("!document.querySelector('.pt-ed')")
        await b.until("document.querySelector('#feed').dataset.ptheme==='1'")
        got['ev'] = await b.js("window.__published.filter(e=>e.kind===16767).pop()")
        got['page'] = await b.js("""(()=>{const f=document.querySelector('#feed');
          return {bg:f.style.getPropertyValue('--bg'), name:getComputedStyle(document.querySelector('.prof .pbody h2')).color,
                  bannerWidth:getComputedStyle(document.querySelector('.prof .banner')).borderTopWidth, preview:window.__prevBg};})()""")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    tags = got['ev']['tags']
    assert tags[:3] == [['c', '#f6f1e7', 'background'], ['c', '#2b2118', 'text'], ['c', '#b4472f', 'primary']], tags
    assert ['c', '#b4472f', 'name'] in tags and ['border', 'none'] in tags and ['client', 'PosterChan'] in tags, tags
    page = got['page']
    assert page['bg'] == '#f6f1e7' and page['name'] == 'rgb(180, 71, 47)' and page['bannerWidth'] == '0px', page
    assert page['preview'] == 'rgb(246, 241, 231)', ("the editor's preview did not show the preset", page)


@CHROME
def test_with_the_switches_off_a_profile_keeps_the_apps_look_and_stays_quiet():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ localStorage.setItem('pc_prof_themes','0'); localStorage.setItem('pc_prof_autoplay','0'); if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        for t in ('mousePressed', 'mouseReleased'):
            await b.call('Input.dispatchMouseEvent', {'type': t, 'x': 700, 'y': 500, 'button': 'left', 'clickCount': 1})
        await b.js("try{ __other(); }catch(_){}")
        await b.until("!!window.__OTHER")
        await b.js("__PC.openProfile(window.__OTHER)")
        await b.until("!!document.querySelector('#feed .prof-track audio')")
        await asyncio.sleep(2)
        got['s'] = await b.js("""(()=>{const f=document.querySelector('#feed'), a=document.querySelector('#feed .prof-track audio');
          return {themed:f.dataset.ptheme||'', bg:f.style.getPropertyValue('--bg'), playing:!!a && !a.paused, chip:!!document.querySelector('.pt-song')};})()""")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['s'] == {'themed': '', 'bg': '', 'playing': False, 'chip': False}, got['s']
