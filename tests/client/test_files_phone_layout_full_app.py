"""Files → All on a PHONE, in the real bundled client: what the user reported, measured.

Reported on Android: "scrolling down, file list bleeds into buttons", "buttons display ugly",
"search field border cut off slightly on the left and right side", "no way to sort by newest".
Each assertion below is one of those, measured at 390px and 360px with 60 files on the drive:

  * after a scroll, the thing painted at the toolbar's rows IS the toolbar -- the selection bar was
    sticky at the same top and z-index and slid over Back/Up/Sort;
  * every toolbar control is inside the screen, and the search box keeps a gutter of at least 6px;
  * no tile button sits on top of the tile's checkbox (`.movebtn` kept an absolute corner position);
  * no ☐ ☑ ⧉ text glyphs in Files -- Android's font has none of them and draws empty boxes;
  * the sort control offers "Newest first", and it is the default.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


DRIVE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
const _driveFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=String(url);
 if(/\/list\/[0-9a-f]{64}/.test(u)){
   const rows=[];for(let i=0;i<60;i++){const h=(i.toString(16).padStart(2,'0')).repeat(32);
     rows.push({sha256:h,size:1000*(i+1),type:i%3?'image/jpeg':'application/pdf',uploaded:1700000000+i*3600,
       url:location.origin+'/'+h+(i%3?'.jpg':'.pdf')});}
   return new Response(JSON.stringify(rows),{status:200,headers:{'Content-Type':'application/json'}});
 }
 return _driveFetch(url,opts);
};
'''

MEASURE = r'''(()=>{
 const vw=innerWidth, bar=document.querySelector('.fx-bar'), find=document.querySelector('#fx-find');
 const out={vw, problems:[]};
 if(!bar||!find){out.problems.push('no toolbar');return out;}
 const br=bar.getBoundingClientRect(), fr=find.getBoundingClientRect();
 // What is actually painted over the toolbar's controls (3 probe rows, 3 columns).
 for(const y of [br.top+18, br.top+(br.height/2), fr.top+fr.height/2])
   for(const x of [20, vw/2, vw-20]){
     const top=document.elementFromPoint(x,y);
     if(top && !bar.contains(top)) out.problems.push('covered at '+Math.round(x)+','+Math.round(y)+' by .'+String(top.className||top.tagName).slice(0,40));
   }
 for(const el of bar.querySelectorAll('button,select,input')){
   if(getComputedStyle(el).display==='none') continue;
   const r=el.getBoundingClientRect(); if(!r.width) continue;
   if(r.left<0||r.right>vw+0.5) out.problems.push('off-screen: '+(el.id||el.className)+' '+Math.round(r.left)+'..'+Math.round(r.right));
 }
 if(fr.left<6||vw-fr.right<6) out.problems.push('search box gutter '+Math.round(fr.left)+'/'+Math.round(vw-fr.right)+'px');
 for(const card of document.querySelectorAll('.file-card')){
   const box=card.querySelector('.selbox'); if(!box) continue;
   const b=box.getBoundingClientRect();
   for(const btn of card.querySelectorAll('button')){
     const r=btn.getBoundingClientRect();
     if(r.width&&r.left<b.right&&r.right>b.left&&r.top<b.bottom&&r.bottom>b.top){
       out.problems.push('tile button .'+btn.className+' covers the checkbox');break;}
   }
   if(out.problems.some(p=>p.startsWith('tile button')))break;
 }
 const pane=document.querySelector('#files-pane')||document.body;
 const glyph=(pane.innerText||'').match(/[☐☑⧉]/);
 if(glyph) out.problems.push('text glyph '+JSON.stringify(glyph[0])+' (tofu on Android)');
 const sel=document.querySelector('#fx-sort');
 out.sortOptions=sel?[...sel.options].map(o=>o.textContent):[];
 out.sortSelected=sel&&sel.selectedOptions[0]?sel.selectedOptions[0].textContent:'';
 return out;})()'''

SCROLL = r'''(()=>{let s=document.querySelector('.fx-bar');
 while(s&&s!==document.documentElement){const o=getComputedStyle(s).overflowY;
   if(/auto|scroll/.test(o)&&s.scrollHeight>s.clientHeight){s.scrollTop+=700;return true}s=s.parentElement}
 window.scrollBy(0,700);return false})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,height', [(390, 844), (360, 780)])
def test_files_all_on_a_phone(width, height):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',
                     {'width': width, 'height': height, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('blossom')")
        await b.until("!!document.querySelector('.fx-home-tile, .fx-bar')")
        # "All": the whole drive.
        await b.js("(()=>{const a=[...document.querySelectorAll('button,[data-folder]')].find(e=>/^\\s*All( files)?\\s*$/i.test(e.textContent)||e.dataset.folder==='');a&&a.click()})()")
        await b.until("document.querySelectorAll('.file-card[data-sha]').length>=30")
        top = await b.js(MEASURE)
        assert top['problems'] == [], top['problems']
        assert top['sortSelected'] == 'Newest first', top
        assert 'Oldest first' in top['sortOptions'] and 'Name A–Z' in top['sortOptions'], top
        await b.js(SCROLL)
        await asyncio.sleep(.3)
        scrolled = await b.js(MEASURE)
        assert scrolled['problems'] == [], scrolled['problems']

    asyncio.run(desktop.with_browser('online', '', check, DRIVE))
