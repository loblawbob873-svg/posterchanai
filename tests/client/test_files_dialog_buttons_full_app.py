"""Files' New folder and New document dialogs, pressed in the SHIPPED bundle:

  * New folder → Cancel closes and makes nothing; Create makes the folder and opens it;
  * New document → Cancel closes and makes nothing.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
         "osMode:false}));")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_new_folder_and_new_document_dialogs():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('blossom'); 1")
        await b.until("!!document.querySelector('#bl-newfolder') && !!document.querySelector('#bl-newdoc')")

        await b.js("document.querySelector('#bl-newfolder').click(); 1")
        await b.until("!!document.querySelector('#nf-cancel')")
        await b.js("document.querySelector('#nf-name').value='Never made'; document.querySelector('#nf-cancel').click(); 1")
        await b.until("!document.querySelector('#nf-cancel')")
        assert not await b.js("/Never made/.test(document.querySelector('#feed').textContent)")

        await b.js("document.querySelector('#bl-newfolder').click(); 1")
        await b.until("!!document.querySelector('#nf-create')")
        await b.js("document.querySelector('#nf-name').value='Receipts 2026'; document.querySelector('#nf-create').click(); 1")
        await b.until("!document.querySelector('#nf-create') && /Receipts 2026/.test(document.querySelector('#feed').textContent)")

        await b.js("document.querySelector('#bl-newdoc').click(); 1")
        await b.until("!!document.querySelector('#nd-cancel')")
        await b.js("document.querySelector('#nd-name').value='Unwritten'; document.querySelector('#nd-cancel').click(); 1")
        await b.until("!document.querySelector('#nd-cancel')")
        assert not await b.js("/Unwritten/.test(document.querySelector('#feed').textContent)")

        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
