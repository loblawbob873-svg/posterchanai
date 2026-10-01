"""Notes: paste or drop a picture or any file into a note and it becomes an attachment.

Asked for: "notes add ability to paste image or whatever in the note as attachment". The 📎 button
already encrypted a file into the Notes folder and referenced it in the body; a paste or a drop did
nothing (an image paste into a textarea is simply lost). Now all three share one path: the reference
goes in AT THE CURSOR for a paste/drop, the note records the attachment and saves, and pasting plain
text is untouched. Runs the shipped Notes in the bundled client; only the upload and the relay are stubbed.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""
window.uploads=[]; __PC.uploadEncFile=async (f)=>{ uploads.push([f.name||'', f.type]); return String(uploads.length).repeat(64).slice(0,64); };
window.published=0; {const real=__PC.publish; __PC.publish=async (...a)=>{ published++; return {ok:true, ev:{id:'x'+published, created_at:Math.floor(Date.now()/1000)}}; };}
window.toasts=[]; __PC.toast=x=>toasts.push(x);
window.fileOf=(name,type,txt)=>new File([txt||'\x89PNG....'],name,{type});
window.dt=(files,text)=>{ const d=new DataTransfer(); for(const f of files) d.items.add(f); if(text) d.setData('text/plain',text); return d; };
window.paste=(files,text)=>{ const ev=new ClipboardEvent('paste',{clipboardData:dt(files,text),bubbles:true,cancelable:true});
  document.querySelector('.nt-body').dispatchEvent(ev); return ev.defaultPrevented; };
window.drop=(files)=>{ const ev=new DragEvent('drop',{dataTransfer:dt(files),bubbles:true,cancelable:true});
  document.querySelector('.nt-body').dispatchEvent(ev); return ev.defaultPrevented; };
"""


async def _note(b, w=1280, h=900):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 600})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('notes')")
    await b.until("!![...document.querySelectorAll('.nt-new')].find(x=>x.getClientRects().length)")
    await b.js(SETUP)
    await b.js("[...document.querySelectorAll('.nt-new')].find(x=>x.getClientRects().length).click()")
    await b.until("!!document.querySelector('.nt-body')")
    await b.js("(()=>{const t=document.querySelector('.nt-body');t.value='Before\\nAfter';t.dispatchEvent(new Event('input',{bubbles:true}));t.focus();t.setSelectionRange(7,7);})()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('w,h', [(1280, 900), (390, 844)])
def test_a_pasted_image_is_attached_at_the_cursor(w, h):
    got = {}

    async def check(b):
        await _note(b, w, h)
        got['prevented'] = await b.js("paste([fileOf('shot.png','image/png')])")
        await b.until("uploads.length===1 && document.querySelector('.nt-body').value.includes('pcres:')")
        await b.until("!!document.querySelector('.nt-res [data-sha]')")
        got['body'] = await b.js("document.querySelector('.nt-body').value")
        got['uploads'] = await b.js("uploads")
        got['saved'] = await b.js("(document.querySelector('.nt-state')||{}).textContent||''")
        got['res'] = await b.js("document.querySelectorAll('.nt-res [data-sha]').length")

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['prevented'] is True and got['uploads'] == [['shot.png', 'image/png']], got
    sha = '1' * 64
    assert got['body'] == f"Before\n![shot.png](pcres:{sha})\nAfter", ("the reference is not at the cursor", got['body'])
    assert 'saved' in got['saved'] and got['res'] == 1, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_pasting_text_is_left_alone_and_a_dropped_file_is_attached():
    got = {}

    async def check(b):
        await _note(b)
        got['text_prevented'] = await b.js("paste([], 'just words')")
        await asyncio.sleep(.3)
        got['uploads_after_text'] = await b.js("uploads.length")
        got['dropped'] = await b.js("drop([fileOf('invoice.pdf','application/pdf','%PDF-1.4'), fileOf('pic.jpg','image/jpeg')])")
        await b.until("uploads.length===2 && (document.querySelector('.nt-body').value.match(/pcres:/g)||[]).length===2")
        got['body'] = await b.js("document.querySelector('.nt-body').value")

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['text_prevented'] is False and got['uploads_after_text'] == 0, "a text paste must be the browser's"
    assert got['dropped'] is True, got
    assert f"[invoice.pdf](pcres:{'1'*64})" in got['body'] and f"![pic.jpg](pcres:{'2'*64})" in got['body'], got['body']
    assert "![invoice.pdf]" not in got['body'], "a PDF is a link, not an image"
