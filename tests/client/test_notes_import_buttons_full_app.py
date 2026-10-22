"""Notes → Import, pressed in the SHIPPED bundle:

  * a folder of .md files is read, the sheet says how many notes it found, and "Import N notes" writes them --
    both appear in the library;
  * "Re-download from the relay" (confirmed) clears this device's copy and reads the notes back;
  * "Delete ALL notes everywhere" asks for DELETE to be typed and then empties the library.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
           "osMode:false}));")
TITLES = "(document.querySelector('#feed')||{}).innerText||''"


async def confirm(b, typed=None):
    await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
    if typed is not None:
        await b.js(f"(()=>{{const i=document.querySelector('.uiprompt-in');i.value={typed!r};i.dispatchEvent(new Event('input',{{bubbles:true}}));}})(); 1")
    await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_import_markdown_redownload_and_delete_everything():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('notes'); 1")
        await b.until("!!document.querySelector('.nt-import')")
        await b.js("document.querySelector('.nt-import').click(); 1")
        await b.until("!!document.querySelector('#nt-imp-md')")
        await b.js("""(()=>{const i=document.querySelector('#nt-imp-md');const dt=new DataTransfer();
            dt.items.add(new File(['# Ferry times\\n\\nFirst at 06:10.'],'Ferry times.md',{type:'text/markdown'}));
            dt.items.add(new File(['# Shopping\\n\\n- eggs'],'Shopping.md',{type:'text/markdown'}));
            i.files=dt.files;i.dispatchEvent(new Event('change'));})(); 1""")
        await b.until("!!document.querySelector('#nt-imp-go') && /Found 2 notes/.test(document.querySelector('#nt-imp-out').textContent)")
        await b.js("document.querySelector('#nt-imp-go').click(); 1")
        await b.until("/Imported 2 notes/.test((document.querySelector('#nt-imp-out')||{}).textContent||'')")
        await b.until("!document.querySelector('#nt-imp-out')")          # the sheet closes itself when done
        await b.until(f"/Ferry times/.test({TITLES}) && /Shopping/.test({TITLES})")

        # The fixture relay acknowledges writes without keeping them; hand it what the import published, which is
        # exactly what a real relay holds, so the re-download has something to bring back.
        await b.js("window.__events=(window.__events||[]).concat(__published.filter(e=>e.kind===30078)); 1")
        await b.js("document.querySelector('.nt-import').click(); 1")
        await b.until("!!document.querySelector('#nt-imp-reset')")
        await b.js("document.querySelector('#nt-imp-reset').click(); 1")
        await confirm(b)
        await b.js("__PC.closeModal && __PC.closeModal(); 1")
        await b.until(f"/Ferry times/.test({TITLES})")

        await b.js("document.querySelector('.nt-import').click(); 1")
        await b.until("!!document.querySelector('#nt-imp-nuke')")
        await b.js("document.querySelector('#nt-imp-nuke').click(); 1")
        await confirm(b, typed="DELETE")
        await b.js("__PC.closeModal && __PC.closeModal(); 1")
        await b.until(f"!/Ferry times|Shopping/.test({TITLES})")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
