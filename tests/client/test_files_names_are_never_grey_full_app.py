"""File and folder NAMES are never drawn grey -- in any source, in tiles or in the list.

Reported twice: "the folder and file names are hard to read, dark grey? really", then "laptop file
manager has gray text for folder and file names but white for Blossom … we should not see gray". Names
had inherited `--muted` (the colour for SECONDARY text: sizes, dates, "Sort by"). The first fix was
tested in Blossom only. This measures every name in Blossom, Synced Folders and My Computer, as tiles
and as a list, in a PosterChanOS window, against the theme's own --text and --muted.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


HOST = r'''
window.pcHost={roots:async()=>[{path:'/home/test',name:'Home',kind:'home'}],
 list:async(path)=>({path,parent:'/home',entries:[{name:'vmtest',path:'/home/test/vmtest',dir:true,size:0,mtime:100},
   {name:'notes.txt',path:'/home/test/notes.txt',dir:false,size:48000,mtime:100}]}),
 read:async()=>new TextEncoder().encode('x'), open:async()=>({ok:true})};
const _nf=window.fetch;
window.fetch=async function(url,opts={}){const u=String(url);
 if(/\/list\/[0-9a-f]{64}/.test(u)){const h='ab'.repeat(32);
   return new Response(JSON.stringify([{sha256:h,size:1234,type:'image/jpeg',uploaded:1700000000,url:location.origin+'/'+h+'.jpg'}]),
     {status:200,headers:{'Content-Type':'application/json'}});}
 return _nf(url,opts);};
window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;
'''

MEASURE = r'''(()=>{const root=getComputedStyle(document.documentElement);
  const norm=v=>{const d=document.createElement('i');d.style.color=v;document.body.appendChild(d);const c=getComputedStyle(d).color;d.remove();return c};
  const text=norm(root.getPropertyValue('--text').trim()), muted=norm(root.getPropertyValue('--muted').trim());
  const names=[...document.querySelectorAll('.fname, .fx-side .folder-chip:not(.active)')].filter(e=>e.getBoundingClientRect().width>0);
  return {text, muted, n:names.length, grey:names.filter(e=>getComputedStyle(e).color===muted).map(e=>e.textContent.trim().slice(0,30))}})()'''


def _view_click(view):
    return "(()=>{const b=document.querySelector('[data-view=\"%s\"].fx-vw');b&&b.click()})()" % view


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('view', ['tiles', 'details'])
def test_no_name_is_grey_in_any_source(view):
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('.fx-side')")
        # My Computer
        await b.until("!!document.querySelector('[data-host=\"1\"]')")
        await b.js("document.querySelector('[data-host=\"1\"]').click()")
        await b.until("!!document.querySelector('[data-p=\"/home/test/vmtest\"]')")
        await b.js(_view_click(view)); await asyncio.sleep(.4)
        got['computer'] = await b.js(MEASURE)
        # Blossom: All files
        await b.js("(()=>{const a=[...document.querySelectorAll('.fx-side [data-folder], .fx-side button')].find(e=>/^\\s*All\\s*$/.test(e.textContent));a&&a.click()})()")
        await b.until("!!document.querySelector('.file-card[data-sha]')")
        await b.js(_view_click(view)); await asyncio.sleep(.4)
        got['blossom'] = await b.js(MEASURE)

    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, HOST))
    for source, m in got.items():
        assert m['text'] != m['muted'], m                       # the theme really has two colours
        assert m['n'] > 0, f"{source}: no names measured -- the selectors went stale: {m}"
        assert not m['grey'], f"{source} ({view}): names drawn in the muted grey: {m['grey']}"
