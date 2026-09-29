"""Media Center → Delete, in the shipped UI: the button, the app's own confirm, the request, the grid.

The server half (the file really leaves the disk, only the owning admin may, nothing outside the
library can be reached) is tests/test_media_center_delete.py. This drives the SHIPPED bundle against a
stub media node and checks what a person sees:

  * a library you manage shows Delete on every title; one shared WITH you shows none;
  * Delete asks with the app's own dialog (never window.confirm), naming the title; Keep sends nothing;
  * confirming sends DELETE /<library>/items/<id> -- an id, never a path -- and the card leaves the
    grid without the list being reloaded, and the library count drops;
  * a refusal shows the server's sentence and the card stays;
  * the same at a phone width (390px, classic layout) and in the windowed desktop, where the dialog
    belongs to the Media Center window.
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
            {id:'lonely',name:'Lonely',folder:'Solo',duration:5400,video:true,progress:{}}];
 const lib=(id,name,manage)=>({id,name,owner:'x',count:items.length,scanned_at:1,skipped:0,encoder:'cpu',can_manage:manage,
   shared_with_me:!manage,scan:{state:'idle'},revision:'r1',folder:'/m',shared_with:[]});
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/media-center');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/media-center','');
  const method=opts.method||'GET';__mcReq.push({path,method});
  if(path===''&&method==='GET') return ok({libraries:[lib('L1','Movies',true),lib('L2','Shared Shows',false)],can_create:true,profiles:['480p'],viewer:'v',unshared:0});
  if(path==='/roots') return ok({roots:['/m']});
  if(path==='/limits') return ok({server_kbps:20000,max_streams:8,max_transcodes:2,cache_mb:2048});
  if(path.startsWith('/jellyfin-account')) return ok({devices:[],server:''});
  if(/\/folders$/.test(path)) return ok({path:'.',folders:[]});
  if(/\/items$/.test(path)) return ok({items,scan:{state:'idle'},revision:'r1'});
  const del=path.match(/^\/L1\/items\/([^/]+)$/);
  if(del&&method==='DELETE'){
    if(del[1]==='film2') return ok({detail:'The media server is not allowed to delete Film Two.mkv (permission denied). Give the PosterChan service write access to that folder on the media server.'},409);
    items=items.filter(i=>i.id!==del[1]);
    return ok({deleted:del[1],status:'deleted',message:'Deleted '+del[1]+'.mkv from disk.'});
  }
  return ok({});
 };})();
'''

PHONE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''


async def _open_library(b, name):
    await b.until("!!document.querySelector('.mc-library-open')")
    await b.js("[...document.querySelectorAll('.mc-library-open')].find(x=>x.textContent.startsWith(%r)).click();true" % name)
    await b.until("!!document.querySelector('#mc-items .mc-tile')")


async def _drive(b, got):
    await desktop.login(b)
    await b.js("__PC.switchView('media-center');true")
    await _open_library(b, 'Movies')
    got['deletes'] = await b.js("document.querySelectorAll('#mc-items .mc-tile .mc-delete').length")
    got['fit'] = await b.js(r'''(()=>{const out=[];for(const card of [...document.querySelectorAll('#mc-items .mc-tile')].filter(c=>c.getClientRects().length)){
        const c=card.getBoundingClientRect();
        for(const el of card.querySelectorAll('.xdc-tacts .btn')){const r=el.getBoundingClientRect();
          if(r.width<24||r.height<24||r.right>c.right+1||r.left<c.left-1||el.scrollWidth>el.clientWidth+1)out.push([card.dataset.item,el.className,Math.round(r.width)]);}}
        return {bad:out, visible:[...document.querySelectorAll('#mc-items .mc-tile')].filter(c=>c.getClientRects().length).length, pageOverflow: document.documentElement.scrollWidth>document.documentElement.clientWidth+1};})()''')

    # Keep: nothing is sent, the card stays.
    await b.js("document.querySelector('.mc-tile[data-item=film1] .mc-delete').click();true")
    await b.until("!!document.querySelector('.uiconfirm')")
    got['question'] = await b.js("document.querySelector('.uiconfirm-msg').textContent")
    got['owned'] = await b.js("(()=>{const o=document.querySelector('.uiconfirm-bg');return {inWindow:!!o.closest('.osw-body'), feedInWindow:!!document.getElementById('feed').closest('.osw-body')};})()")
    got['dialog_fits'] = await b.js(r'''(()=>{const r=document.querySelector('.uiconfirm').getBoundingClientRect();
        return r.left>=-1&&r.right<=document.documentElement.clientWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1;})()''')
    await b.js("document.querySelector('.uiconfirm [data-uc=\"0\"]').click();true")
    await b.until("!document.querySelector('.uiconfirm')")
    got['after_keep'] = await b.js("[!!document.querySelector('.mc-tile[data-item=film1]'), __mcReq.filter(r=>r.method==='DELETE').length]")

    # Delete: DELETE with the id, the card leaves the grid, no reload of the list.
    items_before = await b.js("__mcReq.filter(r=>/\\/items$/.test(r.path)).length")
    await b.js("document.querySelector('.mc-tile[data-item=film1] .mc-delete').click();true")
    await b.until("!!document.querySelector('.uiconfirm')")
    await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click();true")
    await b.until("!document.querySelector('.mc-tile[data-item=film1]')")
    got['del_req'] = await b.js("__mcReq.filter(r=>r.method==='DELETE').map(r=>r.path)")
    got['reloaded'] = (await b.js("__mcReq.filter(r=>/\\/items$/.test(r.path)).length")) != items_before
    got['left'] = await b.js("[...document.querySelectorAll('#mc-items .mc-tile')].map(c=>c.dataset.item)")
    got['count'] = await b.js("[...document.querySelectorAll('.mc-library-open')].find(x=>x.textContent.startsWith('Movies')).textContent")
    await b.until("[...document.querySelectorAll('.toast')].some(t=>/Deleted film1/.test(t.textContent))")

    # The last title of a folder takes its (now empty) section with it.
    await b.js("document.querySelector('#mc-search').value='lonely';document.querySelector('#mc-search').dispatchEvent(new Event('input'));true")
    await b.js("document.querySelector('.mc-tile[data-item=lonely] .mc-delete').click();true")
    await b.until("!!document.querySelector('.uiconfirm')")
    await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click();true")
    await b.until("!document.querySelector('.mc-tile[data-item=lonely]')")
    got['solo_section'] = await b.js("!!document.querySelector('.mc-folder[data-folder=Solo]')")
    await b.js("document.querySelector('#mc-search').value='';document.querySelector('#mc-search').dispatchEvent(new Event('input'));true")

    # A refusal: the server's sentence, and the card stays with its button usable again.
    await b.js("document.querySelector('.mc-tile[data-item=film2] .mc-delete').click();true")
    await b.until("!!document.querySelector('.uiconfirm')")
    await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click();true")
    await b.until("[...document.querySelectorAll('.toast')].some(t=>/permission denied/.test(t.textContent))")
    got['refused'] = await b.js("[!!document.querySelector('.mc-tile[data-item=film2]'), document.querySelector('.mc-tile[data-item=film2] .mc-delete').disabled]")
    got['native'] = await b.js("window.__nativeConfirm||0")

    await b.js("document.getElementById('mc-tab-shared').click();true")
    await _open_library(b, 'Shared Shows')
    got['shared_deletes'] = await b.js("document.querySelectorAll('#mc-items .mc-tile .mc-delete').length")


def _check(got, windowed):
    assert got['deletes'] == 4, got
    assert got['fit']['visible'] == 3 and not got['fit']['bad'] and not got['fit']['pageOverflow'], got['fit']
    assert '“Film One”' in got['question'] and 'disk' in got['question'], got['question']
    if windowed:
        assert got['owned']['feedInWindow'] and got['owned']['inWindow'], got['owned']
    assert got['dialog_fits'], 'the delete question does not fit the screen'
    assert got['after_keep'] == [True, 0], got['after_keep']
    assert got['del_req'] == ['/L1/items/film1'], got['del_req']
    assert not got['reloaded'], 'the list was re-fetched instead of the card leaving the grid'
    assert got['left'] == ['film2', 'song', 'lonely'], got['left']
    assert got['count'].startswith('Movies · 3'), got['count']
    assert got['solo_section'] is False, 'an emptied folder section was left behind'
    assert got['refused'] == [True, False], got['refused']
    assert got['native'] == 0, 'window.confirm was used'
    assert got['shared_deletes'] == 0, 'a library shared WITH this user offers Delete'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_admin_deletes_a_title_in_the_windowed_desktop():
    got = {}
    asyncio.run(desktop.with_browser('online', '', lambda b: _drive(b, got), FIXTURE))
    _check(got, windowed=True)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_admin_deletes_a_title_on_a_phone():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await _drive(b, got)
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + PHONE))
    _check(got, windowed=False)
