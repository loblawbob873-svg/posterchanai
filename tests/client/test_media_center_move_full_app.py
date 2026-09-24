"""Media Center → Move, in the shipped UI: the button, the dialog, the request and the repaint.

The server half (the file really moves, the id is kept) is tests/test_media_center_move.py. This
drives the SHIPPED bundle against a stub media node and checks the part a person sees:

  * a library you manage shows Move on every title; one shared WITH you shows none;
  * the dialog is a FOLDER BROWSER: it opens at the top level showing only the folders there (never
    every nested path at once), a folder is opened by tapping it, the breadcrumb goes back up, and
    "Move here" moves into the folder being looked at -- refused where the title already is;
  * a NEW folder is made inside the folder being looked at, sending exactly {items:[id], folder, create};
  * after the move the same card (same id) shows its new folder -- the list is not rebuilt around it;
  * a refusal from the server is shown in the dialog, which stays open;
  * at a phone width the dialog fits the screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
window.__mcReq=[];
(()=>{const inner=window.fetch;
 const ok=j=>new Response(JSON.stringify(j),{status:200,headers:{'Content-Type':'application/json'}});
 let items=[{id:'film1',name:'Film One',folder:'Inbox',duration:5400,video:true,progress:{}},
            {id:'film2',name:'Film Two',folder:'Movies/Drama',duration:5400,video:true,progress:{}}];
 let rev=1;
 const lib=(id,name,manage)=>({id,name,owner:'x',count:2,scanned_at:1,skipped:0,encoder:'cpu',can_manage:manage,
   shared_with_me:!manage,scan:{state:'idle'},revision:'r'+rev,folder:'/m',shared_with:[]});
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/media-center');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/media-center','');
  const method=opts.method||'GET';__mcReq.push({path,method,body:opts.body||''});
  if(path===''&&method==='GET') return ok({libraries:[lib('L1','Movies',true),lib('L2','Shared Shows',false)],can_create:true,profiles:['480p'],viewer:'v',unshared:0});
  if(path==='/roots') return ok({roots:['/m']});
  if(path==='/limits') return ok({server_kbps:20000,max_streams:8,max_transcodes:2,cache_mb:2048});
  if(path.startsWith('/jellyfin-account')) return ok({devices:[],server:''});
  if(/\/move-targets$/.test(path)) return ok({folders:['.','Empty Folder','Inbox','Movies','Movies/Drama']});
  if(/\/folders$/.test(path)) return ok({path:'.',folders:[{name:'Inbox',path:'Inbox'},{name:'Movies',path:'Movies'}]});
  if(/\/items$/.test(path)) return ok({items,scan:{state:'idle'},revision:'r'+rev});
  if(/\/move$/.test(path)){
    const b=JSON.parse(opts.body);
    if(b.folder==='Taken') return ok({moved:[],errors:[{id:b.items[0],name:'',error:'Film One.mkv already exists there'}],revision:'r'+rev});
    items=items.map(i=>b.items.includes(i.id)?{...i,folder:b.folder}:i);rev++;
    return ok({moved:b.items,errors:[],revision:'r'+rev});
  }
  return ok({});
 };})();
'''


async def _open_library(b, name):
    await b.until("!!document.querySelector('.mc-library-open')")
    await b.js("[...document.querySelectorAll('.mc-library-open')].find(x=>x.textContent.startsWith(%r)).click();true" % name)
    await b.until("!!document.querySelector('#mc-items .mc-tile')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_admin_moves_a_title_from_the_library_view():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('media-center');true")
        await _open_library(b, 'Movies')
        got['moves'] = await b.js("document.querySelectorAll('#mc-items .mc-tile .mc-move').length")
        await b.js("document.querySelector('.mc-tile[data-item=film1] .mc-move').click();true")
        await b.until("!!document.querySelector('.mc-mv-modal')")
        rows = "[...document.querySelectorAll('.mc-mv-dir')].map(r=>r.dataset.path)"
        crumbs = "[...document.querySelectorAll('#mc-mv-crumbs button')].map(b=>b.textContent)"
        got['top'] = await b.js(rows)
        got['top_crumbs'] = await b.js(crumbs)
        got['go_at_top'] = await b.js("[document.getElementById('mc-mv-go').disabled, document.getElementById('mc-mv-go').textContent]")
        # Deeper: open Movies, see only what is inside it.
        await b.js("document.querySelector('.mc-mv-dir[data-path=\"Movies\"]').click();true")
        await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
        got['inside_movies'] = await b.js(rows)
        got['movies_crumbs'] = await b.js(crumbs)
        # The folder it is already in cannot be the destination.
        await b.js("document.querySelector('#mc-mv-crumbs button[data-path=\".\"]').click();true")
        await b.js("document.querySelector('.mc-mv-dir[data-path=\"Inbox\"]').click();true")
        await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
        got['go_in_current'] = await b.js("document.getElementById('mc-mv-go').disabled")

        # A refusal is shown in the dialog, which stays open.
        await b.js("document.querySelector('#mc-mv-crumbs button[data-path=\".\"]').click();true")
        await b.js("document.getElementById('mc-mv-new').value='Taken';document.getElementById('mc-mv-new').dispatchEvent(new Event('input'));document.getElementById('mc-mv-go').click();true")
        await b.until("/already exists/.test(document.getElementById('mc-mv-said').textContent)")
        got['still_open'] = await b.js("!!document.querySelector('.mc-mv-modal')")

        # A new folder INSIDE the folder being looked at.
        await b.js("document.querySelector('.mc-mv-dir[data-path=\"Movies\"]').click();true")
        await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
        await b.js("document.getElementById('mc-mv-new').value='Sci-Fi/';document.getElementById('mc-mv-new').dispatchEvent(new Event('input'));document.getElementById('mc-mv-go').click();true")
        await b.until("!document.querySelector('.mc-mv-modal')")
        await b.until("(document.querySelector('.mc-tile[data-item=film1] .xdc-tfoot')||{}).textContent==='Movies/Sci-Fi'")
        got['move_req'] = await b.js("__mcReq.filter(r=>/\\/move$/.test(r.path)).map(r=>JSON.parse(r.body))")

        # Moving into an existing folder: open it, Move here.
        await b.js("document.querySelector('.mc-tile[data-item=film2] .mc-move').click();true")
        await b.until("!!document.querySelector('.mc-mv-modal')")
        await b.js("document.querySelector('.mc-mv-dir[data-path=\"Inbox\"]').click();true")
        await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
        await b.js("document.getElementById('mc-mv-go').click();true")
        await b.until("(document.querySelector('.mc-tile[data-item=film2] .xdc-tfoot')||{}).textContent==='Inbox'")
        got['last_req'] = await b.js("JSON.parse(__mcReq.filter(r=>/\\/move$/.test(r.path)).pop().body)")

        await b.js("document.getElementById('mc-tab-shared').click();true")
        await _open_library(b, 'Shared Shows')
        got['shared_moves'] = await b.js("document.querySelectorAll('#mc-items .mc-tile .mc-move').length")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert got['moves'] == 2, got
    # Only the TOP level at first -- never every nested path at once.
    assert got['top'] == ['Empty Folder', 'Inbox', 'Movies'], got['top']
    assert got['top_crumbs'] == ['Movies'], got['top_crumbs']
    assert got['go_at_top'] == [False, 'Move here'], got['go_at_top']
    assert got['inside_movies'] == ['Movies/Drama'], got['inside_movies']
    assert got['movies_crumbs'] == ['Movies', 'Movies'], got['movies_crumbs']
    assert got['go_in_current'] is True, 'the folder a title is already in was offered as its destination'
    assert got['still_open'], 'a refused move closed the dialog'
    assert got['move_req'][-1] == {'items': ['film1'], 'folder': 'Movies/Sci-Fi', 'create': True}, got['move_req']
    assert got['move_req'][0] == {'items': ['film1'], 'folder': 'Taken', 'create': True}, got['move_req']
    assert got['last_req'] == {'items': ['film2'], 'folder': 'Inbox', 'create': False}, got['last_req']
    assert got['shared_moves'] == 0, 'a library shared WITH this user offers Move'


PHONE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_move_dialog_fits_a_phone():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('media-center');true")
        await _open_library(b, 'Movies')
        await b.js("document.querySelector('.mc-tile[data-item=film1] .mc-move').click();true")
        await b.until("!!document.querySelector('.mc-mv-modal')")
        await asyncio.sleep(.3)
        got['fit'] = await b.js(r'''(()=>{const vw=document.documentElement.clientWidth, vh=innerHeight;
          const m=document.querySelector('.mc-mv-modal').getBoundingClientRect();
          const over=[...document.querySelectorAll('.mc-mv-modal *')].filter(e=>e.getClientRects().length&&e.getBoundingClientRect().right>vw+1).map(e=>e.className||e.tagName);
          const go=document.getElementById('mc-mv-go').getBoundingClientRect();
          return {vw, left:m.left>=-1, right:m.right<=vw+1, over, goVisible: go.bottom<=vh && go.top>=0, goH: Math.round(go.height)}})()''')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + PHONE))
    f = got['fit']
    assert f['left'] and f['right'] and not f['over'], f
    assert f['goVisible'] and f['goH'] >= 32, f
