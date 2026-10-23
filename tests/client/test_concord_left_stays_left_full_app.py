"""A community the account LEFT stays left -- on a fresh device, and after every write that is not a join.

2026-10-22, live Armada-interop run on production: the test identity left an Armada-made community and, ~10 minutes
later in a fresh browser profile (same key), it was back in the room list. The vault probe and a traced client showed
how a left community gets back into the account's membership vault: `persistArmadaMemberships` stamped EVERY write
`added_at = max(now, tombstone + 1)` and wiped the device's "left" record -- the rule a deliberate RE-JOIN needs,
applied to every caller. Most callers are not joins (the vault backfill, an adopted rekey or dissolution, a staff
grant, a refounding, a new private channel, the instance community's automatic join); they write because a room is
in THIS DEVICE's list, which can be older than the account's decision. Measured live with the shipped client: a
fresh sign-in re-joined the instance community the account had left three minutes earlier, newer than its tombstone.

Real bundled client, a REAL minted community (PosterCord.createCommunity), the real vault code (33302 fragments,
NIP-44, signing) over one in-page relay (tests/client/concord_fixture_relay.py). Each scenario ends on a FRESH
device: no rooms, no "left" ledger, no auto-join marker -- only the account's vault -- and runs the membership pass.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Every membership write lands in the in-page relay, and the home relay takes it (cordWriteMembership requires that).
RELAY_TAKES_WRITES = r"""(()=>{__PC.homeRelay=()=> 'wss://home.invalid';
  __PC.relayPublishTo=async(_r,ev,opts)=>{__relaySeed([ev]);return opts&&opts.detailed?{ok:true}:1;};return true})()"""

ROOMS = "JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]').map(r=>r.name)"

# A second machine on the same account: its own storage is empty; the relay (the vault) is the same.
FRESH_DEVICE_SYNC = r"""(async()=>{const pk=__PC.me().pubkey;
  for(const k of Object.keys(localStorage))if(k.startsWith('pc.concord.')&&k!=='pc.concord.invites')localStorage.removeItem(k);
  await PCConcord.syncArmadaMemberships(__PC,__PC.viewer(),'recovery');
  await PCConcord.syncArmadaMemberships(__PC,__PC.viewer(),'recovery');
  return JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+pk)||'[]').map(r=>r.name);})()"""

# What the account's vault says about the community: does an entry outrank its tombstone?
VAULT_SAYS = r"""(async()=>{const pk=__PC.me().pubkey,cid=__cordRoom.communityId;
  const evs=__relayStore.filter(e=>e.kind===33302&&e.pubkey===pk).sort((a,b)=>b.created_at-a.created_at||(a.id<b.id?-1:1));
  if(!evs.length)return 'no vault';
  const doc=JSON.parse(await __PC.nip44dec(pk,evs[0].content));
  const hex=v=>{const s=String(v||'');if(s.length!==43)return s;const r=atob(s.replace(/-/g,'+').replace(/_/g,'/')+'=');return [...r].map(c=>c.charCodeAt(0).toString(16).padStart(2,'0')).join('');};
  const e=(doc.entries||[]).find(x=>hex(x.community_id)===cid),t=(doc.tombstones||[]).find(x=>hex(x.community_id)===cid);
  return e&&(!t||BigInt(e.added_at)>BigInt(t.removed_at))?'member':t?'left':'unknown';})()"""


async def _start(b, name):
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=850, deviceScaleFactor=1, mobile=False))
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('concord')")
    await b.until("!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord && !!PCConcord.__instanceJoin")
    await b.js(INSTALL)
    await b.js(RELAY_TAKES_WRITES)
    minted = await b.js(MINT.replace("NAME", json.dumps(name)))
    await b.js("window.__cordRoom=%s;true" % json.dumps(minted["room"]))
    return minted["room"]


async def _join_then_leave(b):
    """Device A: a deliberate join (what Accept / Join / Create write), then the real Leave."""
    await b.js("(async()=>{const pk=__PC.me().pubkey;localStorage.setItem('pc.concord.rooms.v1.'+pk,JSON.stringify([__cordRoom]));"
               "await PCConcord.persistArmadaMembership(__PC,__cordRoom,{join:true});return 1})()")
    assert await b.js(VAULT_SAYS) == "member", "fixture: the join did not reach the vault"
    await b.js("PCConcord.leaveArmadaMembership(__PC,__cordRoom).then(()=>1)")
    assert await b.js(VAULT_SAYS) == "left", "fixture: the Leave did not reach the vault"


@CHROME
def test_a_write_that_is_not_a_join_never_brings_a_left_community_back():
    """After the Leave, a device that still holds the room writes it again -- the way an adopted rekey, a dissolution,
    a staff grant, a refounding or a new private channel does (persistArmadaMembership with no `join`). It may not
    outrank the tombstone, a fresh device must not show the community, and a deliberate re-join still works. (The same
    update racing a Leave is kept as a guard: today the Leave's guestbook publish orders it first.)"""
    got = {}

    async def check(b):
        await _start(b, "Left for good")
        await _join_then_leave(b)
        # A stale device writes the room after the Leave (its list still has it).
        await b.js("PCConcord.persistArmadaMembership(__PC,__cordRoom).then(()=>1)")
        got["after_update"] = await b.js(VAULT_SAYS)
        got["fresh_after_update"] = await b.js(FRESH_DEVICE_SYNC)

        # The same update queued behind a Leave still being signed: both start together, the Leave first.
        await b.js("(async()=>{localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([__cordRoom]));"
                   "await PCConcord.persistArmadaMembership(__PC,__cordRoom,{join:true});"
                   "await Promise.all([PCConcord.leaveArmadaMembership(__PC,__cordRoom),PCConcord.persistArmadaMembership(__PC,__cordRoom)]);return 1})()")
        got["after_queued_update"] = await b.js(VAULT_SAYS)
        got["fresh_after_queued_update"] = await b.js(FRESH_DEVICE_SYNC)

        # A deliberate re-join is a join: it outranks the tombstone and reaches a fresh device.
        await b.js("PCConcord.persistArmadaMembership(__PC,__cordRoom,{join:true}).then(()=>1)")
        got["after_rejoin"] = await b.js(VAULT_SAYS)
        got["fresh_after_rejoin"] = await b.js(FRESH_DEVICE_SYNC)
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, _NO_OS))
    assert got["after_update"] == "left", ("an update written after the Leave put the community back in the vault", got)
    assert "Left for good" not in got["fresh_after_update"], ("a fresh device shows the community the account left", got)
    assert got["after_queued_update"] == "left", ("an update queued behind the Leave outranked it", got)
    assert "Left for good" not in got["fresh_after_queued_update"], ("a fresh device shows the community the account left", got)
    assert got["after_rejoin"] == "member", ("a deliberate re-join was swallowed by the old tombstone", got)
    assert "Left for good" in got["fresh_after_rejoin"], ("a re-joined community did not reach a fresh device", got)
    assert not got["errors"], got["errors"]


@CHROME
def test_a_fresh_device_does_not_rejoin_the_instance_community_the_account_left():
    """Measured live on poster.place: a fresh sign-in's automatic instance-community join wrote the membership back
    over the account's tombstone (left 21:49:01, re-added 21:51:40). The marker that made "leaving is final" lives in
    one browser; the vault is the account's."""
    got = {}

    async def check(b):
        await _start(b, "Instance home")
        await _join_then_leave(b)
        # A fresh device on this instance: no marker, no ledger, no rooms. The instance names this community.
        await b.js(r"""(()=>{const pk=__PC.me().pubkey;
          for(const k of Object.keys(localStorage))if(k.startsWith('pc.concord.')&&k!=='pc.concord.invites')localStorage.removeItem(k);
          const url=__cordRoom.url,_f=window.fetch;
          window.fetch=(u,o)=>String(u).endsWith('/client/concord/default-invite')
            ?Promise.resolve(new Response(JSON.stringify({invite:url}),{status:200,headers:{'content-type':'application/json'}})):_f(u,o);
          window.__hydrateHome=async()=>JSON.parse(JSON.stringify(__cordRoom));return true})()""")
        await b.js("PCConcord.__instanceJoin(__hydrateHome).then(()=>1)")
        await asyncio.sleep(.3)
        got["vault"] = await b.js(VAULT_SAYS)
        got["rooms"] = await b.js(ROOMS)
        got["fresh"] = await b.js(FRESH_DEVICE_SYNC)
        # Never left: the same automatic join still joins (the guard against "fixed it by joining nothing").
        await b.js("(()=>{for(const k of Object.keys(localStorage))if(k.startsWith('pc.concord.')&&k!=='pc.concord.invites')localStorage.removeItem(k);return 1})()")
        other = await b.js(MINT.replace("NAME", json.dumps("Instance home 2")))
        await b.js("window.__cordRoom=%s;true" % json.dumps(other["room"]))
        await b.js("PCConcord.__instanceJoin(__hydrateHome).then(()=>1)")
        await b.until(ROOMS + ".includes('Instance home 2')")
        got["joined_vault"] = await b.js(VAULT_SAYS)
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, _NO_OS))
    assert got["vault"] == "left", ("the automatic instance join outranked the account's Leave", got)
    assert "Instance home" not in got["rooms"], ("a fresh device re-joined the instance community the account left", got)
    assert "Instance home" not in got["fresh"], got
    assert got["joined_vault"] == "member", ("the automatic join no longer records a membership", got)
    assert not got["errors"], got["errors"]


_NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
