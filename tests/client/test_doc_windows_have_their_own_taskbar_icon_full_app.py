"""Post, article, profile and search windows show what they are on the taskbar, not the grid square.

"make the icon better when opening a post/thread so it don't have that grid icon on taskbar". On
PosterChanOS each is a real compositor window; the taskbar only knew app icons, so they were anonymous
grids titled 'doc:post:…'. The shipped bundle as the PosterChanOS desktop, with the compositor stood in.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


POST, ART, PROF = "ab" * 32, "cd" * 32, "ef" * 32
WM = r'''
window.__rows=[
  {id:7,app:'place.poster.desktop',title:'PosterChan Desktop',workspace:'1',rect:{x:0,y:0,width:innerWidth,height:innerHeight},focused:false},
  {id:21,app:'place.poster.desktop',title:'PosterChan Window — doc:post:%(post)s',workspace:'1',rect:{x:100,y:60,width:600,height:500},focused:true},
  {id:22,app:'place.poster.desktop',title:'PosterChan Window — doc:post:%(art)s',workspace:'1',rect:{x:140,y:60,width:600,height:500},focused:false},
  {id:23,app:'place.poster.desktop',title:'PosterChan Window — doc:prof:%(prof)s',workspace:'1',rect:{x:180,y:60,width:600,height:500},focused:false},
  {id:24,app:'place.poster.desktop',title:'PosterChan Window — doc:search',workspace:'1',rect:{x:220,y:60,width:600,height:500},focused:false}];
// The article window maps only once the desktop holds the article -- the order openThread uses.
const __live=()=>__rows.filter(r=>r.id!==22||window.__artReady);
window.pcWM={windows:async()=>__live(),launch:async()=>({pid:1}),snapshot:async()=>({windows:__live(),allIds:__live().map(r=>r.id),shellId:7}),
  onEvent:(cb)=>{window.__emit=cb;return()=>{}},focus:async()=>true,shellFront:async()=>true,hasAppWindow:()=>false};
''' % {"post": POST, "art": ART, "prof": PROF}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_each_document_window_has_its_own_icon_and_name():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-start') && window.PCOSShell && PCOSShell.available()===true")
        await b.js("Store.saveEvent({id:'%s',kind:30023,pubkey:'%s',created_at:1,tags:[['d','x'],['title','An article']],content:'# Body',sig:''});window.__artReady=true;window.__emit&&__emit({name:'window',payload:{change:'new'}});true" % (ART, PROF))
        await b.until("document.querySelectorAll('.os-task[data-kind=\"native\"]').length>=4")
        await asyncio.sleep(.6)
        got['tasks'] = await b.js("Object.fromEntries([...document.querySelectorAll('.os-task[data-kind=\"native\"]')].map(t=>[t.dataset.id,{icon:(t.querySelector('use')||{getAttribute:()=>''}).getAttribute('href'),label:(t.querySelector('span')||{}).textContent}]))")

    asyncio.run(desktop.with_browser('online', '', check, WM))
    t = got['tasks']
    assert t['21'] == {'icon': '#i-note', 'label': 'Post'}, t
    assert t['22'] == {'icon': '#i-article', 'label': 'Article'}, t
    assert t['23'] == {'icon': '#i-user', 'label': 'Profile'}, t
    assert t['24'] == {'icon': '#i-search', 'label': 'Search'}, t
    assert not any(v['icon'] == '#i-grid' for v in t.values()), t
