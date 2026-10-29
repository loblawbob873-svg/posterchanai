"""An Office document attached to a note opens in PosterChan Office, and Save puts the edit back INTO the note.

"notes on android not opening office docs in office" (2026-10-29): anything that was not a picture, video or PDF went
to the browser route -- on Android the system share sheet -- never to our Office. The shipped Notes and Files in the
bundled client; the Office bridge (pcOfficeLocal, as the desktop and PosterChanOS provide it) is a stub that records
what it was handed and hands back edited bytes on Save; uploads, the encrypted-file URL and the relay are stubbed.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_notes_paste_and_drop_attach_full_app import _note

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


OFFICE = r"""
window.__docBytes = new TextEncoder().encode('PK-original-docx');
window.__blobs = {}; window.uploads = [];
__PC.uploadEncFile = async (f) => { const sha = String(uploads.length + 1).repeat(64).slice(0, 64);
  uploads.push([f.name || '', f.type, await f.text()]); __blobs[sha] = f; return sha; };
// The harness's network stub answers {} for any address it does not know -- blob: ones included -- so the
// decrypted-file URLs this test hands out are answered here, with their real bytes.
window.__urls = {};
__PC.encFileUrl = async (sha) => { const u = 'blob:test/' + sha; __urls[u] = __blobs[sha] || new Blob([__docBytes]); return u; };
{ const f0 = window.fetch; window.fetch = (u, o) => __urls[String(u)] ? Promise.resolve(new Response(__urls[String(u)])) : f0(u, o); }
window.__office = [];
window.pcOfficeLocal = { installed: () => true, available: async () => true,
  open: async (bytes, name, mode) => { __office.push({ name, mode, text: new TextDecoder().decode(new Uint8Array(bytes)) });
    return { ok: true, id: 's1', token: 't1', editor_url: 'about:blank', expires: 0 }; },
  contents: async () => ({ ok: true, bytes: new TextEncoder().encode('PK-edited-docx') }),
  export: async () => ({ ok: false }), close: async () => ({ ok: true }) };
window.__shared = 0; __PC.nativeOpenBlob = async () => { __shared++; return true; };
"""


@CHROME
def test_a_docx_in_a_note_opens_in_office_and_save_puts_the_edit_back_in_the_note():
    got = {}

    async def check(b):
        await _note(b)
        await b.js(OFFICE)
        await b.js("drop([new File([__docBytes], 'Report.docx', {type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'})])")
        await b.until("!!document.querySelector('.nt-res-item[data-sha]')")
        got['before'] = await b.js("document.querySelector('.nt-body').value")
        await b.js("document.querySelector('.nt-res-item[data-sha]').click()")
        await b.until("__office.length===1")
        got['opened'] = await b.js("__office[0]")
        await b.until("!!document.querySelector('#office-save')")
        await b.js("document.querySelector('#office-save').click()")
        await b.until("uploads.length===2")
        await asyncio.sleep(0.5)
        got['uploads'] = await b.js("uploads")
        # Office took the screen; back to Notes, the same note, and what it SAVED.
        await b.js("__PC.switchView('notes')")
        await b.until("!!document.querySelector('.nt-item')")
        await b.js("document.querySelector('.nt-item').click()")
        await b.until("!!document.querySelector('.nt-body')")
        got['after'] = await b.js("document.querySelector('.nt-body').value")
        got['toasts'] = await b.js("toasts")
        got['shared'] = await b.js("__shared")

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['opened'] == {'name': 'Report.docx', 'mode': 'edit', 'text': 'PK-original-docx'}, got['opened']
    assert got['shared'] == 0, "it went to the share sheet instead of Office"
    assert got['uploads'][1][0] == 'Report.docx' and got['uploads'][1][2] == 'PK-edited-docx', got['uploads']
    old, new = '1' * 64, '2' * 64
    assert 'pcres:' + old in got['before'] and 'pcres:' + new in got['after'] and 'pcres:' + old not in got['after'], got
