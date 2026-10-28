"""A COMMUNITY'S LIVE STREAMS ARE DIALLED ONCE, NOT ONCE A MINUTE.

Measured on poster.place 2026-10-28 (the week jskitty blocked this node for "churning connections"): of the tunnels
to jskitty.com, nostr.computingcache.com and asia.vectorapp.io, 66 in ten minutes were live room subscriptions
(`REQ 1059`, one author, `since`) that OUR CLIENT closed after a 60 s median and opened again. A live subscription
has no reason to end while the community is unchanged. The real bundled client, a real community, this node unable
to serve the streams (so they are read from the community's relays through the tunnel), and five minutes of the page's
clock: every live stream REQ sent in that window is counted. The foreground client passes (the 60 s closes on
production come from another platform -- the tunnel report now names the client kind); this keeps it that way.
"""
import asyncio
import json

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_concord_reads_this_node_full_app as base
from tests.client import test_concord_talks_only_to_this_node_full_app as T

RELAYS = base.CORD_RELAYS

SEND_LOG = r'''(()=>{const send=WebSocket.prototype.send;window.__frames=[];
  WebSocket.prototype.send=function(d){try{if(String(this.url).includes('/ws/concord-relay'))
    __frames.push({u:new URL(this.url).searchParams.get('u'),sock:(this.__sid=this.__sid||Math.random().toString(36).slice(2,7)),at:Date.now(),d:String(d).slice(0,400)});}catch(_){}
    return send.apply(this,arguments);};return true;})()'''

# Live stream REQs sent through the tunnel since the mark -- production's churning shape exactly: one author, `since`,
# NO limit (a history read carries a limit).
LIVE_SINCE_MARK = r'''(window.__frames||[]).slice(window.__fmark||0).filter(x=>{try{const m=JSON.parse(x.d);
  return m[0]==='REQ'&&m.slice(2).some(f=>(f.kinds||[]).includes(1059)&&(f.authors||[]).length===1&&'since' in f&&!('limit' in f));}catch(_){return false;}}).length'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The device's envelope cache refuses one control event (as it does an event over MAX_EVENT_BYTES, or after eviction).
REFUSE_ONE = r'''(()=>{const C=window.PCConcordCache,put=C.put,get=C.get;window.__refused=null;
  const keep=evs=>(evs||[]).filter(e=>{if(!e||e.kind!==1059)return true;if(!window.__refused)window.__refused=e.id;return e.id!==window.__refused;});
  C.put=(k,evs,o)=>put(k,String(k).includes('control')?keep(evs):evs,o);
  C.get=async(...a)=>{const r=await get(...a);return String(a[0]).includes('control')?keep(r):r;};return true;})()'''
# ...and the community's relays hold its control events (a real community's relays do).
ON_RELAYS = r'''(urls=>{for(const u of urls)for(const ev of __home.filter(e=>e.kind===1059))__upHeld.push({url:u,ev});return true;})(%s)'''


@pytest.mark.skipif(not T.CHROME, reason='Chrome required')
@pytest.mark.parametrize('cache', ['whole', 'refuses-one'])
def test_live_streams_are_not_redialled_every_minute(cache):
    """'refuses-one': the community's relays hold a control event this device's cache does not. Inside the metadata
    read's once-a-minute rate limit a refresh got only the cache back, so the control set -- and with it every live
    stream's key -- flipped each minute: the 60 s redial measured on production."""
    got = {}
    from tests.client.test_concord_reads_only_the_relays_this_node_cannot_full_app import MINT as M, REFUSER
    mint = M.replace(json.dumps(base.CORD_RELAYS + [REFUSER]), json.dumps(RELAYS)).replace("'Partly here'", "'Live here'")

    async def check(b):
        await base.open_concord(b)
        assert await b.js(SEND_LOG)
        if cache == 'refuses-one':
            assert await b.js(REFUSE_ONE)
        assert await b.js(mint)
        assert await b.js(ON_RELAYS % json.dumps(RELAYS))
        await b.js('PCConcord.render()')
        await base.wait_for(b, LIVE_SINCE_MARK + '>0', 60)   # the community's live streams are open
        await asyncio.sleep(10)
        got['before'] = await b.js(LIVE_SINCE_MARK)
        await b.js("window.__fmark=(window.__frames||[]).length")
        got['virtual_ms'] = await desktop.advance(b, 300000)
        got['live_reqs'] = await b.js(LIVE_SINCE_MARK)
        got['all'] = await b.js("(window.__frames||[]).slice(window.__fmark||0).filter(x=>x.d.includes('since')&&!x.d.includes('limit')).map(x=>[x.sock,Math.round((x.at-(window.__frames[window.__fmark]||{at:0}).at)/1000),x.d.slice(0,40),x.st])")

    asyncio.run(desktop.with_browser('online', '', check, T.tunnel(__upRelays=RELAYS, __homeMirror='unavailable')))
    assert got['virtual_ms'] >= 299999, ('the page clock did not run the full window', got['virtual_ms'])
    assert got['before'] > 0, ('no live stream was ever opened -- the count below would pass vacuously', got)
    assert got['live_reqs'] == 0, ('live room streams were opened AGAIN while nothing about the community changed '
                                   '(five minutes)', got['live_reqs'], got['all'][:20])
