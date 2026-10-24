"""Dissolving a community takes it OFF Discover.

Reported 2026-10-23: "you need to delete all the pc-federation-canary rooms on Discover, you are polluting the armada
discover". The live federation canary creates a public community and dissolves it at the end of every run -- and
dissolving never touched the community's Discover LISTING (a public kind-1 with the invite URL, tagged concord +
community, sent to the owner's relays and Armada's discovery relay). Ten dissolved canary rooms were still being
offered to every Armada user. Any owner who dissolved a public community left the same thing behind.

The shipped client's dissolveCommunity, with only the network replaced: it must delete (NIP-09) every listing the
owner signed for THAT invite -- the copy this device remembers and one another device signed -- on Armada's
discovery relay as well as the owner's relays, and nothing else the owner posted; and a withdrawal that fails must
never undo or block the dissolution.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

SETUP = r"""(()=>{
  const PK=__PC.me().pubkey, URL='https://poster.place/invite/naddr1dissolvetest#k';
  const ev=(id,content,tags)=>({id:id.repeat(64).slice(0,64),pubkey:PK,kind:1,created_at:1,content,tags,sig:'s'.repeat(128)});
  const held=ev('a','canary room\n\n'+URL,[['t','concord'],['t','community']]);
  localStorage.setItem('pc.concord.listing.v1.'+PK+'.'+URL,JSON.stringify(held));
  window.__got={queried:[],published:[],sentTo:[],dissolved:0};
  const other=ev('b','canary room\n\n'+URL,[['t','concord'],['t','community']]);          // signed on another device
  const unrelated=ev('c','my other community\n\nhttps://poster.place/invite/naddr1other',[['t','concord']]);
  window.PosterCordReader={...(window.PosterCordReader||{}),
    createDissolutionWrap:async()=>({wrap:{id:'w'.repeat(64),pubkey:'f'.repeat(64),kind:1059,tags:[],content:'',created_at:1,sig:''}})};
  window.__p=(failQuery,failPublish)=>({...__PC.concordDeps?__PC.concordDeps():{},
    viewer:()=>({pubkey:PK}), toast:()=>{}, enc:x=>String(x),
    relayUrls:()=>['wss://mine.example'],
    relayQueryFrom:async(relays,filters)=>{__got.queried.push({relays,filters}); if(failQuery)throw Error('down'); return [other,unrelated];},
    publish:async(kind,content,tags)=>{ if(failPublish)throw Error('refused');
      const e={id:'d'.repeat(64),pubkey:PK,kind,content,tags,created_at:2,sig:'s'.repeat(128)}; __got.published.push(e); return {ev:e}; },
    relayPublishTo:async(relays,e)=>{__got.sentTo.push({relays,kind:e.kind}); return true;},
    relayPublishRoom:async()=>{__got.dissolved++; return {ok:true};},
  });
  window.__room={name:'canary room',url:URL,communityId:'c'.repeat(64),naddr:'naddr1dissolvetest',
    cord:{bundle:{owner:PK,relays:['wss://cord.example']}}};
  return PK;
})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _run(fail_query=False, fail_publish=False):
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('concord')")
        await b.until("!!window.PCConcord && typeof PCConcord.dissolveCommunity==='function'")
        got["pk"] = await b.js(SETUP)
        got["threw"] = await b.js("PCConcord.dissolveCommunity(__p(%s,%s),__room).then(()=>null,e=>String(e&&e.message||e))"
                                  % (json.dumps(fail_query), json.dumps(fail_publish)))
        got["got"] = await b.js("__got")
        got["held_left"] = await b.js("localStorage.getItem('pc.concord.listing.v1.'+__PC.me().pubkey+'.'+__room.url)")

    asyncio.run(desktop.with_browser("online", "", check, ""))
    return got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_dissolving_withdraws_every_listing_of_that_invite_from_discover():
    got = _run()
    assert got["threw"] is None, got
    g = got["got"]
    assert g["dissolved"] == 1, ("the dissolution itself was not sent", g)
    dels = [e for e in g["published"] if e["kind"] == 5]
    assert len(dels) == 1, ("dissolving did not withdraw the Discover listing", g)
    ids = {t[1] for t in dels[0]["tags"] if t[0] == "e"}
    assert ids == {"a" * 64, "b" * 64}, ("the withdrawal must name THIS invite's listings (this device's and another's) and nothing else", ids)
    assert ["k", "1"] in dels[0]["tags"]
    assert any("wss://relay.dreamith.to" in s["relays"] and s["kind"] == 5 for s in g["sentTo"]), \
        ("the withdrawal never reached Armada's discovery relay", g["sentTo"])
    assert any("wss://relay.dreamith.to" in q["relays"] and "wss://mine.example" in q["relays"] for q in g["queried"]), g["queried"]
    assert got["held_left"] is None, "the remembered listing would be re-sent by the next 'Publish listing'"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_unreachable_relay_still_withdraws_the_listing_this_device_holds():
    got = _run(fail_query=True)
    assert got["threw"] is None, got
    dels = [e for e in got["got"]["published"] if e["kind"] == 5]
    assert dels and {t[1] for t in dels[0]["tags"] if t[0] == "e"} == {"a" * 64}, got["got"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_failed_withdrawal_never_blocks_the_dissolution():
    got = _run(fail_publish=True)
    assert got["threw"] is None, ("a refused withdrawal turned a done dissolution into an error", got)
    assert got["got"]["dissolved"] == 1, got
