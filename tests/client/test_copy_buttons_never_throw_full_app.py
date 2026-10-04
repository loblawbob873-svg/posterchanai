"""Copy and Close on a ✨ summary, and Telegram's copy button, never throw.

Reported: "Telegram -> Summarize Link -> You get summary then you get error -> failed to execute
querySelector on 'Document'", "happens with copy and close". A document-wide [data-copy] handler spliced
the attribute into a selector ('#' + value). The summary sheet kept its whole TEXT in data-copy, so every
click inside it -- Copy and Close alike -- threw; a Telegram message's copy button carries its message
NUMBER there, and '#123' is not a valid selector either. Real clicks in the shipped client, every
uncaught error recorded; the summary is what lands on the clipboard.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""(()=>{window.__errs=[];window.__copied=[];
addEventListener('error',e=>__errs.push(String((e.error&&e.error.message)||e.message)));
addEventListener('unhandledrejection',e=>__errs.push(String((e.reason&&e.reason.message)||e.reason)));
const cv=__PC.copyValue;__PC.copyValue=(v,...a)=>{__copied.push(String(v));return Promise.resolve(true);};
const rf=window.fetch;window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist')){ const b=JSON.parse(o.body);
  const body=b.probe?{ok:true,allowed:true}:{ok:true,links:[{url:'https://example.com/a',title:'Example page',summary:'It says hello.'}]};
  return Promise.resolve(new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}})); } return rf(u,o); };
return true;})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("mode", ["classic", "desktop"])
def test_copy_and_close_on_a_summary_and_a_numbered_copy_button(mode):
    res = {}

    async def check(b):
        await desktop.login(b)
        if mode == "classic":
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await asyncio.sleep(.4)
        await b.js(SETUP)
        for btn in ("ca-copy", "ca-close"):
            await b.js("PCChatAssist.summarizeLinks('look https://example.com/a','telegram');true")
            await b.until("!!document.querySelector('.ca-sheet .ca-link')")
            await b.js(f"document.querySelector('.ca-sheet .{btn}').click();true")
            await asyncio.sleep(.4)
            res[btn] = await b.js("({errs:__errs.splice(0),copied:__copied.splice(0),open:!!document.querySelector('.ca-sheet')})")
            await b.js("try{__PC.closeModal()}catch(_){};true")
        # Telegram's per-message copy button: data-copy is the message NUMBER.
        await b.js("(()=>{const x=document.createElement('button');x.className='tg-mini';x.dataset.copy='123';x.textContent='c';document.body.appendChild(x);x.click();})();true")
        await asyncio.sleep(.3)
        res["numbered"] = await b.js("__errs.splice(0)")

    asyncio.run(desktop.with_browser("online", "", check))
    assert res["ca-copy"]["errs"] == [] and res["ca-close"]["errs"] == [], res
    assert "It says hello." in "".join(res["ca-copy"]["copied"]), ("Copy did not copy the summary", res)
    assert res["ca-close"]["open"] is False, ("Close did not close the sheet", res)
    assert res["numbered"] == [], res
