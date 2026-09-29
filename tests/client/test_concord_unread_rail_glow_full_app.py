"""A community with new messages glows on the left rail; one you have read does not.

Asked for: "It would be good for the some kind a glow or something when a community you are in gets
new messages. I am talking about the icons on the left". The only signal was a 5px tick beside the
icon. Real bundled client and stylesheet; rooms are local fixtures, so no relay is involved.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""(()=>{
  const me=__PC.me().pubkey;
  const mk=(n,id)=>({name:n,naddr:id,communityId:id.repeat(64).slice(0,64),local:true,channels:[{id:id+'-general',name:'general'}]});
  localStorage.setItem('pc.concord.invites',JSON.stringify([mk('Quiet','a'),mk('Busy','b')]));
  localStorage.setItem('pc.concord.active','0');
  __PC.switchView('concord');
})()"""

GLOW = r"""(()=>[...document.querySelectorAll('.cc-communities .cc-server[data-cc-server], .cc-communities .cc-server:not(#cc-discovery)')]
  .map(b=>({name:b.getAttribute('title')||b.getAttribute('aria-label')||'',unread:b.classList.contains('unread'),
            shadow:getComputedStyle(b).boxShadow}))
  .filter(x=>/Quiet|Busy/.test(x.name)))()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_a_community_with_new_messages_glows_on_the_rail(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        if width < 600:
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("!!document.querySelector('.cc-communities')")
        # A message arrives in the room you are NOT looking at.
        await b.js("""(()=>{const key=Object.keys(localStorage).find(k=>k.startsWith('pc.concord.test.')&&/b/.test(k))||'pc.concord.test.b';
            localStorage.setItem('pc.concord.test.b',JSON.stringify([{id:'n1',by:'Other',pubkey:'c'.repeat(64),text:'new!',at:Date.now(),kind:9}]));
            PCConcord.render();})()""")
        await asyncio.sleep(.3)
        rows = await b.js(GLOW)
        busy = next((r for r in rows if "Busy" in r["name"]), None)
        quiet = next((r for r in rows if "Quiet" in r["name"]), None)
        assert busy and quiet, rows
        assert busy["unread"], f"the room with a new message is not marked unread: {rows}"
        assert busy["shadow"] != "none", f"the unread community does not glow: {busy}"
        assert quiet["shadow"] == "none", f"a read community glows: {quiet}"

    asyncio.run(desktop.with_browser("online", "", check))
