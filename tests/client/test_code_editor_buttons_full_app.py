"""PosterChan Code's toolbar and find bar, pressed in the SHIPPED bundle on a drive document opened the way
Files → Open in Code opens one (PCCode.openBlob):

  * Find opens the find bar; ↓ / ↑ walk the matches; ▸ shows Replace and "Replace" changes the current match;
    × closes it;
  * Format sends the document to this node's formatter and puts its answer in the buffer;
  * Reload on a DRIVE document goes back to its stored version -- it never asks the node's workspace for a file
    of the same name (it used to: an error, or somebody else's file in this tab).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


TEXT = '{"name":"demo","tags":["foo","bar","foo"]}'
PRETTY = '{\n    "name": "demo",\n    "tags": [\n        "foo",\n        "bar",\n        "foo"\n    ]\n}\n'

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__code=[];
const __cf=window.fetch;
window.fetch=(u,o)=>{const s=String(u);if(!s.includes('/api/code/'))return __cf(u,o);
  __code.push(s.split('/api/code')[1]);
  const json=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(s.includes('/api/code/format'))return json({ok:true,changed:true,text:PRETTY,source:PRETTY,formatted:PRETTY});
  if(s.includes('/api/code/file'))return json({text:'SOMEBODY ELSE\'S FILE',mtime:1});
  return json({ok:true,engines:{},files:[],folder:''});};
'''.replace('PRETTY', json.dumps(PRETTY))

TA = "document.querySelector('.pcc-ed textarea, textarea.pcc-ta, #pcc-ta, .pcc textarea')"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_find_replace_format_and_reload_of_a_drive_document():
    async def check(b):
        await desktop.login(b)
        await b.js(f"(async()=>{{const m=await __PC.withModule?.('code.js','PCCode');}})(); 1")
        await b.js("__PC.switchView('code'); 1")
        await b.until("!!window.PCCode && !!PCCode.openBlob")
        await b.js(f"PCCode.openBlob({{sha:'c'.repeat(64),name:'data.json',text:{json.dumps(TEXT)}}}); __PC.switchView('code'); 1")
        await b.until(f"!!{TA} && {TA}.value==={json.dumps(TEXT)}")

        # Find: three-letter query, two matches; next/prev move the selection; replace the current one.
        await b.js("document.querySelector('#pcc-find-b').click(); 1")
        await b.until("!!document.querySelector('#pcc-f-q')")
        await b.js("(()=>{const q=document.querySelector('#pcc-f-q');q.value='foo';q.dispatchEvent(new Event('input',{bubbles:true}));})(); 1")
        await b.until("/2/.test(document.querySelector('#pcc-f-n').textContent)")
        sel = f"{TA}.selectionStart"
        await b.js("document.querySelector('#pcc-f-next').click(); 1")
        first = await b.js(sel)
        await b.js("document.querySelector('#pcc-f-next').click(); 1")
        await b.until(f"{sel}!=={first}")
        await b.js("document.querySelector('#pcc-f-prev').click(); 1")
        await b.until(f"{sel}==={first}")
        await b.js("document.querySelector('#pcc-f-mode').click(); 1")
        await b.until("!!document.querySelector('#pcc-f-r1')")
        await b.js("(()=>{const r=document.querySelector('#pcc-f-r');r.value='baz';r.dispatchEvent(new Event('input',{bubbles:true}));"
                   "document.querySelector('#pcc-f-r1').click();})(); 1")
        await b.until(f"(({TA}.value.match(/foo/g)||[]).length===1) && {TA}.value.includes('baz')")
        await b.js("document.querySelector('#pcc-f-x').click(); 1")
        await b.until("!document.querySelector('#pcc-find')")

        # Reload a DRIVE document: back to its stored text, and the workspace is never asked.
        await b.js("document.querySelector('#pcc-reload').click(); 1")
        await b.until(f"{TA}.value==={json.dumps(TEXT)}")
        assert not await b.js("__code.some(p=>p.startsWith('/file'))"), "Reload read a workspace file for a drive document"

        # Format: the node's formatter answers and the buffer takes it.
        await b.until("!document.querySelector('#pcc-fmt').disabled")
        await b.js("document.querySelector('#pcc-fmt').click(); 1")
        await b.until(f"{TA}.value==={json.dumps(PRETTY)}")
        assert await b.js("__code.some(p=>p.startsWith('/format'))")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
