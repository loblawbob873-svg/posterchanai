"""Discover drops a community listing its author deleted -- and only its author.

Reported 2026-10-24: "i still see a lot of your concord test rooms" in PosterChan's Discover, after every one of those
listings had been deleted (NIP-09) on every relay that held them. Discover is fed by this node's directory AND the
device's own relay pool and cache, and it never applied a deletion, so a copy the device had seen stayed on offer.

The shipped client, real keys and signatures (NostrTools in the page): the listing's author's deletion withdraws it;
a deletion by somebody else, and a forged one that claims the author's pubkey, withdraw nothing.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHECK = r"""(async()=>{
  const NT=NostrTools, a=NT.generateSecretKey(), b=NT.generateSecretKey(), A=NT.getPublicKey(a);
  const listing=(sk,name)=>NT.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000),
      tags:[['t','concord'],['t','community']],content:name+'\n\nhttps://poster.place/invite/naddr1'+name},sk);
  const gone=listing(a,'dissolved'), kept=listing(a,'alive'), theirs=listing(b,'someone-elses');
  const del=(sk,ids)=>NT.finalizeEvent({kind:5,created_at:Math.floor(Date.now()/1000),tags:[...ids.map(i=>['e',i]),['k','1']],content:''},sk);
  const real=del(a,[gone.id]);
  const byOther=del(b,[kept.id]);                                  // b cannot withdraw a's listing
  const forged={...del(b,[theirs.id]),pubkey:A};                     // claims to be a, signed by b
  const answers=[real,byOther,forged];
  const asked=[];
  const p={relayQueryFrom:async(relays,filters)=>{asked.push({relays,filters});return answers;}};
  const rows=[gone,kept,theirs].map(ev=>({source:ev}));
  const n=await PCConcord.readListingDeletions(p,rows,null);
  return {n,withdrawn:PCConcord.withdrawnListings(),ids:{gone:gone.id,kept:kept.id,theirs:theirs.id},asked};
})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_only_the_authors_own_signed_deletion_withdraws_a_listing():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('concord')")
        await b.until("!!window.PCConcord && typeof PCConcord.readListingDeletions==='function' && !!window.NostrTools")
        got["r"] = await b.js(CHECK)

    asyncio.run(desktop.with_browser("online", "", check, ""))
    r = got["r"]
    assert r["withdrawn"] == [r["ids"]["gone"]], ("the wrong listings were withdrawn", r)
    assert r["n"] == 1, r
    f = r["asked"][0]["filters"][0]
    assert f["kinds"] == [5] and set(f["#e"]) == set(r["ids"].values()), ("the deletions were not asked for these listings", r)
    assert "wss://relay.dreamith.to" in r["asked"][0]["relays"], r["asked"]


def test_discover_leaves_a_withdrawn_listing_out_of_what_it_paints():
    """The paint path filters on the same set (a listing withdrawn before or after it arrived)."""
    src = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text()
    on_event = src[src.index("const onEvent=ev=>{if(signal&&signal.aborted)return;for(const item of discoverInvites("):][:600]
    assert "withdrawnListings.has(" in on_event
    assert "readListingDeletions(p,listings,signal)" in src
