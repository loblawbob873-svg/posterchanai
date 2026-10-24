"""Media Center → Move folder, in the shipped UI.

Reported 2026-10-24: "No way to move folders. Moving files support only". The server half (the directory moves,
the titles keep their ids) is tests/test_media_center_move_folder.py. This drives the SHIPPED bundle against a
stub media node:

  * inside a folder of a library you manage, the breadcrumb offers "Move folder…"; at the top level, and in a
    library shared WITH you, it does not -- and the current-folder crumb is still the one marked current;
  * the dialog is the same folder browser, and never offers the folder itself or anything inside it;
  * Move here sends exactly {folder, to, create} to /move-folder, and the screen then shows the folder where it
    went (breadcrumb and all);
  * a refusal is shown in the dialog, which stays open.
"""
import asyncio
import json
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
 const bad=(s,d)=>new Response(JSON.stringify({detail:d}),{status:s,headers:{'Content-Type':'application/json'}});
 let dirs=['Inbox','Inbox/Extras','Movies','Movies/Drama','Shelf','Shelf/Inbox2'];
 let items=[{id:'film1',name:'Film One',folder:'Inbox',duration:5400,video:true,progress:{}},
            {id:'film2',name:'Film Two',folder:'Inbox/Extras',duration:5400,video:true,progress:{}}];
 let rev=1;
 const lib=(id,name,manage)=>({id,name,owner:'x',count:2,scanned_at:1,skipped:0,encoder:'cpu',can_manage:manage,
   shared_with_me:!manage,scan:{state:'idle'},revision:'r'+rev,folder:'/m',shared_with:[]});
 const childrenOf=p=>dirs.filter(d=>(p==='.'?!d.includes('/'):d.startsWith(p+'/')&&!d.slice(p.length+1).includes('/'))).map(d=>({name:d.slice(d.lastIndexOf('/')+1),path:d,art:[]}));
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/media-center');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/media-center','');
  const method=opts.method||'GET';__mcReq.push({path,method,body:opts.body||'',query:p.search});
  if(path===''&&method==='GET') return ok({libraries:[lib('L1','Movies',true),lib('L2','Shared Shows',false)],can_create:true,profiles:['480p'],viewer:'v',unshared:0});
  if(path==='/roots') return ok({roots:['/m']});
  if(path==='/limits') return ok({server_kbps:20000,max_streams:8,max_transcodes:2,cache_mb:2048});
  if(path.startsWith('/jellyfin-account')) return ok({devices:[],server:''});
  if(/\/move-targets$/.test(path)) return ok({folders:['.'].concat(dirs)});
  if(/\/folders$/.test(path)){ const at=p.searchParams.get('path')||'.';
    if(at!=='.'&&!dirs.includes(at)) return bad(400,'That folder does not exist');
    return ok({path:at,folders:childrenOf(at)}); }
  if(/\/items$/.test(path)) return ok({items,scan:{state:'idle'},revision:'r'+rev});
  if(/\/move-folder$/.test(path)){
    const b=JSON.parse(opts.body);
    if(b.to==='Shelf') return bad(400,'Inbox already exists there');
    const name=b.folder.slice(b.folder.lastIndexOf('/')+1), to=b.to==='.'?name:b.to+'/'+name;
    const mv=x=>x===b.folder?to:x.startsWith(b.folder+'/')?to+x.slice(b.folder.length):x;
    if(b.create&&!dirs.includes(b.to)) dirs.push(b.to);
    dirs=dirs.map(mv); items=items.map(i=>({...i,folder:mv(i.folder)})); rev++;
    return ok({moved:2,path:to,revision:'r'+rev});
  }
  return ok({});
 };})();
'''

CRUMBS = "[...document.querySelectorAll('.mc-folder-trail button:not(.mc-move-folder)')].map(b=>b.textContent)"
CURRENT = "(document.querySelector('.mc-folder-trail [aria-current=page]')||{}).textContent||''"
MOVE_BTN = "!!document.querySelector('.mc-folder-trail .mc-move-folder')"
ROWS = "[...document.querySelectorAll('.mc-mv-dir')].map(r=>r.dataset.path)"


async def _open_library(b, name):
    await b.until("!!document.querySelector('.mc-library-open')")
    await b.js("[...document.querySelectorAll('.mc-library-open')].find(x=>x.textContent.startsWith(%r)).click();true" % name)
    await b.until("(document.querySelector('.mc-folder-trail button')||{}).textContent===%s && !!document.querySelector('#mc-folder-nav .mc-directory')" % json.dumps(name))


async def _browse(b, path):
    await b.js("document.querySelector(%s).click();true" % json.dumps('.mc-directory[data-path="%s"]' % path))
    await b.until("[...document.querySelectorAll('.mc-folder-trail button:not(.mc-move-folder)')].length>1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_admin_moves_a_whole_folder_from_the_library_view():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('media-center');true")
        await _open_library(b, 'Movies')
        got['at_top'] = await b.js(MOVE_BTN)
        await _browse(b, 'Inbox')
        await b.until(MOVE_BTN)
        await asyncio.sleep(.3)
        got['current'] = await b.js(CURRENT)
        await b.js("document.querySelector('.mc-move-folder').click();true")
        await b.until("!!document.querySelector('.mc-mv-modal')")
        got['title'] = await b.js("document.querySelector('.mc-mv-modal h3').textContent")
        got['top_rows'] = await b.js(ROWS)
        # The folder it is in (the top level) is not a destination.
        got['go_at_home'] = await b.js("document.getElementById('mc-mv-go').disabled")
        # A refusal stays in the dialog.
        await b.js("document.querySelector('.mc-mv-dir[data-path=\"Shelf\"]').click();true")
        await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
        await b.js("document.getElementById('mc-mv-go').click();true")
        await b.until("/already exists/.test(document.getElementById('mc-mv-said').textContent)")
        got['still_open'] = await b.js("!!document.querySelector('.mc-mv-modal')")
        # Into Movies → a new folder 2024 inside it.
        await b.js("document.querySelector('#mc-mv-crumbs button[data-path=\".\"]').click();true")
        await b.js("document.querySelector('.mc-mv-dir[data-path=\"Movies\"]').click();true")
        await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
        await b.js("document.getElementById('mc-mv-new').value='2024';document.getElementById('mc-mv-new').dispatchEvent(new Event('input'));document.getElementById('mc-mv-go').click();true")
        await b.until("!document.querySelector('.mc-mv-modal')")
        await b.until("%s.join('/')==='Movies/Movies/2024/Inbox'" % CRUMBS)
        got['req'] = await b.js("__mcReq.filter(r=>/\\/move-folder$/.test(r.path)).map(r=>JSON.parse(r.body))")
        got['after'] = await b.js(CRUMBS)
        got['shown'] = await b.js("[...document.querySelectorAll('#mc-folder-nav .mc-directory')].map(d=>d.dataset.path)")
        await b.js("document.getElementById('mc-tab-shared').click();true")
        await _open_library(b, 'Shared Shows')
        await _browse(b, 'Shelf')
        await asyncio.sleep(.3)
        got['shared_btn'] = await b.js(MOVE_BTN)

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert got['at_top'] is False, 'the top level of a library offered Move folder'
    assert got['current'] == 'Inbox', ('the Move folder button stole the current-folder mark', got['current'])
    assert got['title'] == 'Move folder “Inbox”', got['title']
    assert 'Inbox' not in got['top_rows'] and set(got['top_rows']) == {'Movies', 'Shelf'}, got['top_rows']
    assert got['go_at_home'] is True, 'the folder it is already in was offered as the destination'
    assert got['still_open'], 'a refused move closed the dialog'
    assert got['req'][0] == {'folder': 'Inbox', 'to': 'Shelf', 'create': False}, got['req']
    assert got['req'][-1] == {'folder': 'Inbox', 'to': 'Movies/2024', 'create': True}, got['req']
    assert got['after'] == ['Movies', 'Movies', '2024', 'Inbox'], got['after']
    assert got['shown'] == ['Movies/2024/Inbox/Extras'], got['shown']
    assert got['shared_btn'] is False, 'a library shared WITH this user offers Move folder'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_folder_is_never_offered_a_place_inside_itself():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('media-center');true")
        await _open_library(b, 'Movies')
        await _browse(b, 'Movies')
        await b.until(MOVE_BTN)
        await b.js("document.querySelector('.mc-move-folder').click();true")
        await b.until("!!document.querySelector('.mc-mv-modal')")
        got['rows'] = await b.js(ROWS)
        # Typing a path into itself is refused before any request.
        await b.js("document.getElementById('mc-mv-new').value='Movies/Drama';document.getElementById('mc-mv-new').dispatchEvent(new Event('input'));true")
        got['typed_self'] = await b.js("[document.getElementById('mc-mv-go').disabled, document.getElementById('mc-mv-said').textContent]")
        got['sent'] = await b.js("__mcReq.filter(r=>/\\/move-folder$/.test(r.path)).length")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert 'Movies' not in got['rows'] and 'Movies/Drama' not in got['rows'], got['rows']
    assert got['typed_self'][0] is True and 'inside itself' in got['typed_self'][1], got['typed_self']
    assert got['sent'] == 0


PHONE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_move_folder_button_fits_a_phone_deep_in_a_library():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('media-center');true")
        await _open_library(b, 'Movies')
        await _browse(b, 'Movies')
        await b.js("document.querySelector(%s).click();true" % json.dumps('.mc-directory[data-path="Movies/Drama"]'))
        await b.until("[...document.querySelectorAll('.mc-folder-trail button:not(.mc-move-folder)')].length===3")
        await b.until(MOVE_BTN)
        await asyncio.sleep(.3)
        got['fit'] = await b.js(r'''(()=>{const vw=document.documentElement.clientWidth;
          const m=document.querySelector('.mc-move-folder').getBoundingClientRect();
          return {vw, inside:m.left>=0&&m.right<=vw+1, h:Math.round(m.height), pageScroll:document.documentElement.scrollWidth>vw+1}})()''')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + PHONE))
    f = got['fit']
    assert f['inside'] and f['h'] >= 28 and not f['pageScroll'], f
