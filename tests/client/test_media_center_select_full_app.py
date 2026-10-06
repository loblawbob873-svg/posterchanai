"""Media Center → Select: all, none, or a few, then Move or Delete them together.

"allow selecting all, selecting none, selecting few, etc so you can better move/delete files".
Drives the SHIPPED bundle against a stub media node, in the windowed desktop and at phone width:

  * a library you manage has a Select button; one shared WITH you does not;
  * in select mode every tile carries a checkbox, its own Play/Move/Delete buttons go away, and a tap
    on a tile PICKS it -- it never starts playback;
  * Select all picks what is on screen (this folder), Select none clears, Shift-click picks a run;
  * Delete… asks once with the app's own dialog naming the count, sends ONE request with exactly the
    picked ids, removes the cards that went and NAMES the one the server refused;
  * Move… opens the folder browser titled with the count and sends every picked id in one move.
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
window.confirm=()=>{window.__nativeConfirm=(window.__nativeConfirm||0)+1;return true;};
(()=>{const inner=window.fetch;
 const ok=(j,s=200)=>new Response(JSON.stringify(j),{status:s,headers:{'Content-Type':'application/json'}});
 let items=[{id:'film1',name:'Film One',folder:'.',duration:5400,video:true,progress:{}},
            {id:'film2',name:'Film Two',folder:'.',duration:5400,video:true,progress:{}},
            {id:'song',name:'A Song',folder:'.',duration:200,video:false,progress:{}},
            {id:'clip',name:'A Clip',folder:'.',duration:60,video:true,progress:{}},
            {id:'lonely',name:'Lonely',folder:'Solo',duration:5400,video:true,progress:{}}];
 const lib=(id,name,manage)=>({id,name,owner:'x',count:items.length,scanned_at:1,skipped:0,encoder:'cpu',can_manage:manage,
   shared_with_me:!manage,scan:{state:'idle'},revision:'r1',folder:'/m',shared_with:[]});
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/media-center');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/media-center','');
  const method=opts.method||'GET';const body=opts.body?JSON.parse(opts.body):null;__mcReq.push({path,method,body});
  if(path===''&&method==='GET') return ok({libraries:[lib('L1','Movies',true),lib('L2','Shared Shows',false)],can_create:true,profiles:['480p'],viewer:'v',unshared:0});
  if(path==='/roots') return ok({roots:['/m']});
  if(path==='/limits') return ok({server_kbps:20000,max_streams:8,max_transcodes:2,cache_mb:2048});
  if(path.startsWith('/jellyfin-account')) return ok({devices:[],server:''});
  if(/\/move-targets$/.test(path)) return ok({folders:['Solo']});
  if(/\/folders$/.test(path)) return ok({path:'.',folders:[]});
  if(/\/items$/.test(path)) return ok({items,scan:{state:'idle'},revision:'r1'});
  if(path==='/L1/delete'&&method==='POST'){
    const deleted=body.items.filter(id=>id!=='song');
    items=items.filter(i=>!deleted.includes(i.id));
    return ok({deleted,errors:body.items.includes('song')?[{id:'song',name:'A Song',error:'permission denied'}]:[],revision:'r2'});
  }
  if(path==='/L1/move'&&method==='POST'){
    for(const i of items) if(body.items.includes(i.id)) i.folder=body.folder;
    return ok({moved:body.items,errors:[],revision:'r3'});
  }
  if(/\/play\//.test(path)) return ok({url:'/x/master.m3u8'});
  return ok({});
 };})();
'''

PHONE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''

COUNT = "(document.querySelector('.mc-sel-count')||{}).textContent||''"
PICKED = "[...document.querySelectorAll('#mc-items .mc-tile.mc-picked')].map(c=>c.dataset.item)"


async def _open_library(b, name):
    await b.until("!!document.querySelector('.mc-library-open')")
    await b.js("[...document.querySelectorAll('.mc-library-open')].find(x=>x.textContent.startsWith(%r)).click();true" % name)
    await b.until("!!document.querySelector('#mc-items .mc-tile')")


async def _sel(b, what):
    await b.js("document.querySelector('#mc-selbar [data-sel=%s]').click();true" % what)
    await asyncio.sleep(.05)


async def _tap(b, item, shift=False):
    """A real pointer click in the middle of the tile's cover."""
    r = await b.js("(()=>{const e=document.querySelector('.mc-tile[data-item=%s] .xdc-cover');e.scrollIntoView({block:'center'});"
                   "const r=e.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()" % item)
    await asyncio.sleep(.1)
    mods = 8 if shift else 0
    for t in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", {"type": t, "x": r["x"], "y": r["y"], "button": "left", "clickCount": 1, "modifiers": mods})
    await asyncio.sleep(.05)


async def _drive(b, got):
    await desktop.login(b)
    await b.js("__PC.switchView('media-center');true")
    await _open_library(b, 'Movies')
    got['bar_before'] = await b.js("[...document.querySelectorAll('#mc-selbar [data-sel]')].map(x=>x.dataset.sel)")
    await _sel(b, 'start')
    got['mode'] = await b.js(r'''(()=>{const tiles=[...document.querySelectorAll('#mc-items .mc-tile')].filter(c=>c.getClientRects().length);
        return {boxes:tiles.filter(c=>{const p=c.querySelector('.mc-pick');return p&&p.getClientRects().length}).length, tiles:tiles.length,
                buttons:tiles.filter(c=>[...c.querySelectorAll('.xdc-tacts .btn')].some(x=>x.getClientRects().length)).length}})()''')
    got['bar_fits'] = await b.js(r'''(()=>{const vw=document.documentElement.clientWidth;
        return [...document.querySelectorAll('#mc-selbar .btn')].every(x=>{const r=x.getBoundingClientRect();return r.left>=-1&&r.right<=vw+1&&r.height>=34&&x.scrollWidth<=x.clientWidth+1})
          && document.documentElement.scrollWidth<=vw+1})()''')
    got['disabled_empty'] = await b.js("['none','move','delete'].map(k=>document.querySelector('#mc-selbar [data-sel='+k+']').disabled)")

    # All = what is on screen (the top folder), None = nothing.
    await _sel(b, 'all')
    got['all'] = [await b.js(COUNT), sorted(await b.js(PICKED))]
    await _sel(b, 'none')
    got['none'] = [await b.js(COUNT), await b.js(PICKED)]

    # A few: tap one, shift-tap another (the run between), tap one off again. No playback.
    plays = "__mcReq.filter(r=>/\\/play\\//.test(r.path)).length"
    await _tap(b, 'film1')
    await _tap(b, 'song', shift=True)
    got['range'] = sorted(await b.js(PICKED))
    await _tap(b, 'film2')
    got['few'] = [await b.js(COUNT), sorted(await b.js(PICKED))]
    got['plays'] = await b.js(plays)

    # Delete the two: one question, one request, the refused one named and kept.
    await _sel(b, 'delete')
    await b.until("!!document.querySelector('.uiconfirm')")
    got['question'] = await b.js("document.querySelector('.uiconfirm-msg').textContent")
    await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click();true")
    await b.until("!document.querySelector('.mc-tile[data-item=film1]')")
    got['del_req'] = await b.js("__mcReq.filter(r=>r.path==='/L1/delete').map(r=>r.body.items.slice().sort())")
    await b.until("[...document.querySelectorAll('.toast')].some(t=>/A Song/.test(t.textContent))")
    got['toast'] = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).find(t=>/A Song/.test(t))")
    got['left'] = await b.js("[...document.querySelectorAll('#mc-items .mc-tile')].map(c=>c.dataset.item)")
    got['after_delete_mode'] = await b.js("document.getElementById('mc-items').classList.contains('mc-selecting')")

    # Move two into a folder: the dialog counts them, one request carries both.
    await _sel(b, 'start')
    await _tap(b, 'film2')
    await _tap(b, 'clip')
    await _sel(b, 'move')
    await b.until("!!document.querySelector('.mc-mv-modal')")
    got['move_title'] = await b.js("document.querySelector('.mc-mv-modal h3').textContent")
    await b.js("document.querySelector('.mc-mv-dir[data-path=\"Solo\"]').click();true")
    await b.until("document.querySelectorAll('#mc-mv-crumbs button').length===2")
    await b.js("document.getElementById('mc-mv-go').click();true")
    await b.until("!document.querySelector('.mc-mv-modal')")
    got['move_req'] = await b.js("__mcReq.filter(r=>r.path==='/L1/move').map(r=>[r.body.items.slice().sort(),r.body.folder])")
    got['native'] = await b.js("window.__nativeConfirm||0")

    await b.js("document.getElementById('mc-tab-shared').click();true")
    await _open_library(b, 'Shared Shows')
    got['shared_bar'] = await b.js("(()=>{const s=document.getElementById('mc-selbar');return !!s&&!s.hidden&&s.getClientRects().length>0})()")


def _check(got):
    assert got['bar_before'] == ['start'], got['bar_before']
    assert got['mode']['boxes'] == got['mode']['tiles'] == 4 and got['mode']['buttons'] == 0, got['mode']
    assert got['bar_fits'], 'the select bar does not fit the screen'
    assert got['disabled_empty'] == [True, True, True], got['disabled_empty']
    assert got['all'] == ['4 selected', ['clip', 'film1', 'film2', 'song']], got['all']   # not `lonely`, in Solo
    assert got['none'] == ['0 selected', []], got['none']
    assert got['range'] == ['film1', 'film2', 'song'], got['range']
    assert got['few'] == ['2 selected', ['film1', 'song']], got['few']
    assert got['plays'] == 0, 'a tap in select mode started playback'
    assert '2 titles' in got['question'] and 'disk' in got['question'], got['question']
    assert got['del_req'] == [['film1', 'song']], got['del_req']
    assert 'Deleted 1 of 2' in got['toast'] and 'permission denied' in got['toast'], got['toast']
    assert 'film1' not in got['left'] and 'song' in got['left'], got['left']
    assert got['after_delete_mode'] is False
    assert got['move_title'] == 'Move 2 titles', got['move_title']
    assert got['move_req'] == [[['clip', 'film2'], 'Solo']], got['move_req']
    assert got['native'] == 0, 'window.confirm was used'
    assert got['shared_bar'] is False, 'a library shared WITH this user offers Select'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_select_all_none_and_a_few_in_the_windowed_desktop():
    got = {}
    asyncio.run(desktop.with_browser('online', '', lambda b: _drive(b, got), FIXTURE))
    _check(got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_select_all_none_and_a_few_on_a_phone():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await _drive(b, got)
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + PHONE))
    _check(got)
