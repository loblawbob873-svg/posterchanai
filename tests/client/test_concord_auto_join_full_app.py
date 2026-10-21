"""Every member of the instance is joined to its community — once, and leaving is final.

Vyram, 2026-10-21 ("Posterchan Community Not Loading"): "If new npubs created on posterchan are suppose to
auto join the community then they aren't." Nothing did. Now Admin -> Nostr Relay names the community, a
signed-in member's client fetches the invite from `/client/concord/default-invite` and joins it the way a
manual join does — and remembers it did, so a member who LEAVES is not pulled back in, while a CHANGED
invite (a new community, or a re-founded one) is joined once more.

Real bundled client; the instance endpoint and the invite reader are fixtures (minting and reading a real
community over relays is covered by the join tests).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


URL1 = "https://poster.place/invite/naddr1qqhm#secret1"
URL2 = "https://poster.place/invite/naddr1qqhm2x#secret2"

FIX = r"""
window.__invite = %s;
const _f = window.fetch;
window.fetch = (u, o) => String(u).endsWith('/client/concord/default-invite')
  ? Promise.resolve(new Response(JSON.stringify({invite: window.__invite}), {status: 200, headers: {'content-type': 'application/json'}}))
  : _f(u, o);
window.__hydrate = async (p, url) => ({name: url.includes("qqhm2x") ? 'PosterChan 2' : 'PosterChan', communityId: url.includes("qqhm2x") ? 'pc-home-2' : 'pc-home-1',
  naddr: url.includes("qqhm2x") ? 'naddr1qqhm2x' : 'naddr1qqhm', url, relays: [], channels: [{name: 'general'}]});
true
"""

ROOMS = "JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]').map(r=>r.name)"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_members_join_the_instance_community_once(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=860, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.switchView('concord'); true")
        await b.until("!!window.PCConcord && !!PCConcord.__autoJoin")
        await b.js(FIX % ('"' + URL1 + '"'))
        assert await b.js(ROOMS) == [], "fixture: a new account starts with no communities"

        # A brand-new account: joined, and the room is opened because nothing else was.
        await b.js("PCConcord.__autoJoin(__hydrate)")
        await b.until(ROOMS + ".includes('PosterChan')")
        await b.until("/PosterChan/.test(document.querySelector('.cc-channels header')?.textContent||'')")
        over = await b.js("document.scrollingElement.scrollWidth-innerWidth")
        assert over <= 1, f"the joined community scrolls sideways by {over}px"

        # Running again changes nothing (no duplicate room).
        await b.js("PCConcord.__autoJoin(__hydrate)")
        await asyncio.sleep(.3)
        assert await b.js(ROOMS) == ["PosterChan"]

        # The member LEAVES (their list no longer has it): the next session must not re-join them.
        await b.js("localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,'[]'); true")
        await b.js("PCConcord.__autoJoin(__hydrate)")
        await asyncio.sleep(.5)
        assert await b.js(ROOMS) == [], "a member who left the instance community was joined to it again"

        # The operator names a DIFFERENT community: joined once more.
        await b.js("window.__invite=%s; true" % ('"' + URL2 + '"'))
        await b.js("PCConcord.__autoJoin(__hydrate)")
        await b.until(ROOMS + ".includes('PosterChan 2')")

        # Blank setting: nothing happens.
        await b.js("localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,'[]');window.__invite=''; true")
        await b.js("localStorage.removeItem('pc.concord.autojoin.v1.'+__PC.me().pubkey); PCConcord.__autoJoin(__hydrate)")
        await asyncio.sleep(.5)
        assert await b.js(ROOMS) == []

    asyncio.run(desktop.with_browser("online", "", check))
