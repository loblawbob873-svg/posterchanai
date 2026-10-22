"""Web Search's reader buttons, pressed in the SHIPPED bundle against a stubbed search API:

  * a result's "Read here" opens the reader with the page's text;
  * the reader's Share opens the composer with the page's title and address;
  * the reader's Notes saves the page's TEXT (not just a bookmark) and its address to Notes.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
const __wf=window.fetch;
window.fetch=(u,o)=>{const s=String(u),json=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(s.includes('/api/websearch/search'))return json({results:[{title:'Ferry timetable',url:'https://ferries.example/timetable',content:'Sailings every hour'}]});
  if(s.includes('/api/websearch/read'))return json({content:'The first ferry leaves at 06:10 and the last at 23:40.'});
  if(s.includes('/api/websearch/'))return json({});
  return __wf(u,o);};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_reader_share_and_save_to_notes():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__notes=[]; __PC.switchView('websearch'); 1")
        await b.until("!!document.querySelector('#ws-q')")
        await b.js("(()=>{const q=document.querySelector('#ws-q');q.value='ferry';document.querySelector('#ws-form').requestSubmit();})(); 1")
        await b.until("!!document.querySelector('.ws-read[data-i=\"0\"]')")
        await b.js("document.querySelector('.ws-read[data-i=\"0\"]').click(); 1")
        await b.until("!!document.querySelector('#ws-r-share') && /06:10/.test(document.querySelector('#feed').textContent)")

        await b.js("document.querySelector('#ws-r-share').click(); 1")
        await b.until("!!document.querySelector('#cmp-send')")
        text = await b.js("document.querySelector('#cmp-send').closest('.modal, .modal-card, [role=dialog], body').querySelector('textarea').value")
        assert "Ferry timetable" in text and "https://ferries.example/timetable" in text, text
        await b.js("__PC.closeModal(); 1")
        await b.until("!document.querySelector('#cmp-send')")

        await b.js("window.PCNotes=Object.assign(window.PCNotes||{},{save:async n=>{__notes.push(n);return {};}}); 1")
        await b.js("document.querySelector('#ws-r-note').click(); 1")
        await b.until("__notes.length===1")
        note = await b.js("__notes[0]")
        assert "06:10" in note["body"] and "https://ferries.example/timetable" in note["body"], note
        assert note["title"] == "Ferry timetable", note
        await b.until("/in Notes/.test(document.querySelector('#ws-r-note').textContent)")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
