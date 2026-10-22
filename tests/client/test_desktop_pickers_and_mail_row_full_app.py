"""The windowed desktop's own sheets, in the SHIPPED bundle: right-click the wallpaper → "Add a widget…",
"Add a program…" and "Change background…" each open their sheet, and its ✕ closes it; and with unread email,
the notification centre's mail row opens Mail.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def desk_menu(b, label):
    await b.js("(()=>{const d=document.getElementById('os-desk');const r=d.getBoundingClientRect();"
               "d.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:r.right-40,clientY:r.bottom-60}));})(); 1")
    await b.until("!!document.querySelector('.os-ctx .os-ctx-b')")
    await b.js("[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>x.textContent.startsWith(%r)).click(); 1" % label)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_widget_program_and_background_sheets_close_and_the_mail_row_opens_mail():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.getElementById('os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on'); 1")
        # "Add a program…" is offered only on PosterChanOS, where installed programs exist to add: stand in its
        # shell with one installed program.
        await b.js("window.PCOSShell=Object.assign(window.PCOSShell||{},{available:()=>true,allApps:async()=>[{id:'firefox',name:'Firefox',exec:'firefox',icon:''}]}); 1")
        for label, x in (("Add a widget", "os-wgt-x"), ("Add a program", "os-app-x"), ("Change background", "os-bg-x")):
            await desk_menu(b, label)
            await b.until(f"!!document.getElementById({x!r})")
            await b.js(f"document.getElementById({x!r}).click(); 1")
            await b.until(f"!document.getElementById({x!r})")

        # Two unread emails: the notification centre says so, and its row opens Mail.
        await b.js("__PC.mailUnread=()=>2; document.getElementById('os-clock').click(); 1")
        await b.until("!!document.getElementById('os-noti-mail')")
        await b.js("document.getElementById('os-noti-mail').click(); 1")
        await b.until("[...document.querySelectorAll('.osw')].some(w=>/mail/i.test((w.querySelector('.osw-title')||{}).textContent||'')) || document.body.dataset.view==='mail'")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check))
