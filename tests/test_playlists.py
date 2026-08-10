"""Music playlists — the encrypted per-playlist store.

One kind-30078 event per playlist (`d = pcai:playlist:<id>`), NIP-44-encrypted to the user's own
key, tagged `l = pcai-music`. Notes' shape, for Notes' reasons: one document holding every playlist
is a read-modify-write of all of them per save, and an index document is a second source of truth
one empty read can wipe.

EVERY TEST HERE IS A BUG THE FIRST DRAFT SHIPPED. A review caught them before deploy; without these
they come back silently, and each one loses data rather than erroring:

  * Relay.publish ALWAYS resolves an object, so `!!(await publish(ev))` is true for a TIMEOUT — the
    offline queue cleared itself on a send that never happened.
  * A tombstone that deletes the map entry can be undone by an older copy arriving later in the same
    batch, because the "is what I have newer?" guard then has nothing to compare against.
  * The kind-5 deletes every event for the address at or below its own created_at, so publishing it
    AFTER the tombstone deletes the tombstone — and an offline device never learns of the delete.
  * create() returning a playlist whose save was refused puts one on screen that exists nowhere.
  * The client cache evicts newest-N by created_at; without a pin exemption a few minutes of feed
    reading drops the whole library off the device.

The store is driven for real under node against stubbed Relay/Store/__PC, so these are behaviours
rather than greps.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "js", "client", "playlists.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const PUB='a'.repeat(64);
const published=[];        // every event handed to publish(), in order
let relayOk=true;          // what Relay.publish reports
let queryEvents=[];        // what the relay hands back on load()
const localStore={};
const ctx={ console, setTimeout, clearTimeout, JSON, Math, Date, Object, Array, String, Number,
  Promise, Map, Set, RegExp, Error, TextEncoder, TextDecoder, isNaN, parseInt };
ctx.window=ctx; ctx.globalThis=ctx; ctx.self=ctx;
ctx.localStorage={ getItem:k=>(k in localStore?localStore[k]:null),
                   setItem:(k,v)=>{localStore[k]=String(v);}, removeItem:k=>{delete localStore[k];} };
ctx.addEventListener=()=>{};
ctx.Store=()=>({});
ctx.Store={ query:()=>[], saveEvent:()=>{} };
ctx.Relay={ query: async()=>queryEvents.slice(),
            subscribe:()=>null, close:()=>{},
            publish: async(ev)=>({ ok:relayOk, msg: relayOk?'':'timeout' }) };
let clock=1000;
ctx.__PC={
  me:()=>({pubkey:PUB}),
  toast:m=>{ (ctx.__toasts=ctx.__toasts||[]).push(String(m)); },
  // A stand-in cipher: reversible, so a round-trip is a real one, and JSON-safe.
  nip44enc: async(pk,pt)=>'E:'+Buffer.from(pt).toString('base64'),
  nip44dec: async(pk,ct)=>Buffer.from(String(ct).slice(2),'base64').toString(),
  publish: async(kind, content, tags, opts)=>{
    const ev={ kind, content, tags, created_at: ++clock, id:'ev'+clock, pubkey:PUB };
    published.push(ev);
    return relayOk ? { ok:true, ev } : { ok:false, ev };
  },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'), ctx, {filename:'playlists.js'});
const P=ctx.PCPlaylists;
const dOf=ev=>((ev.tags||[]).find(t=>t[0]==='d')||[])[1]||'';
const enc=o=>'E:'+Buffer.from(JSON.stringify(o)).toString('base64');
const out={};
(async()=>{
  const CASE=process.argv[3];

  if(CASE==='reorder'){
    out.mid=P.reorder(['a','b','c','d'],0,2);
    out.same=P.reorder(['a','b','c'],1,1);
    out.clampHigh=P.reorder(['a','b','c'],0,99);
    out.clampLow=P.reorder(['a','b','c'],2,-5);
    out.empty=P.reorder([],0,1);
    out.len=P.reorder(['a','b','c'],0,2).length;
  }

  if(CASE==='queue_keeps_failed_send'){
    relayOk=false;                       // publish refuses → the edit is queued
    await P.load();
    const pl=await P.create('Road trip');
    out.createdWhileOffline = pl===null;              // refused, so nothing is handed back
    out.queued = JSON.parse(localStore['pcaiPlaylistsPending']||'[]').length;
    // Now the relay accepts nothing but still RESOLVES — the shape that fooled the first draft.
    await P.flush();
    out.afterFailedFlush = JSON.parse(localStore['pcaiPlaylistsPending']||'[]').length;
    relayOk=true;
    await P.flush();
    out.afterGoodFlush = JSON.parse(localStore['pcaiPlaylistsPending']||'[]').length;
  }

  if(CASE==='tombstone_wins'){
    await P.load();
    const pl=await P.create('Gone');
    const d='pcai:playlist:'+pl.id;
    // Two relays disagreeing: the tombstone is NEWER, an old copy arrives after it in one batch.
    queryEvents=[
      { kind:30078, content:'', tags:[['d',d],['l','pcai-music']], created_at:5000, id:'t', pubkey:PUB },
      { kind:30078, content:enc({name:'Gone',tracks:[],created:1,updated:1}),
        tags:[['d',d],['l','pcai-music']], created_at:4000, id:'o', pubkey:PUB },
    ];
    await P.load(true);
    out.afterTombstone = P.all().map(p=>p.name);
    out.getIsNull = P.get(pl.id)===null;
    // …and a genuinely NEWER edit must still be able to bring it back.
    queryEvents.push({ kind:30078, content:enc({name:'Back',tracks:[],created:1,updated:9}),
      tags:[['d',d],['l','pcai-music']], created_at:6000, id:'n', pubkey:PUB });
    await P.load(true);
    out.afterNewer = P.all().map(p=>p.name);
  }

  if(CASE==='delete_order'){
    await P.load();
    const pl=await P.create('Bye');
    published.length=0;
    await P.remove(pl.id);
    out.kinds = published.map(e=>e.kind);
    out.tombstoneIsLast = published[published.length-1].kind===30078
                       && published[published.length-1].content==='';
    const k5=published.find(e=>e.kind===5), tomb=published.find(e=>e.kind===30078);
    out.kind5Earlier = !!(k5 && tomb && k5.created_at < tomb.created_at);
  }

  if(CASE==='rollback'){
    await P.load();
    const pl=await P.create('Mix');
    out.made=!!pl;
    await P.add(pl.id, 'b'.repeat(64));
    out.oneTrack = P.get(pl.id).tracks.length;
    relayOk=false;                       // from here every save is refused-and-queued
    const n=await P.add(pl.id, 'c'.repeat(64));
    out.addedWhileOffline=n;             // queued counts as kept, so this is 1
    relayOk=true;
    out.finalTracks = P.get(pl.id).tracks.length;
  }

  if(CASE==='too_big'){
    await P.load();
    const pl=await P.create('Huge');
    const many=Array.from({length:2000},(_,i)=>i.toString(16).padStart(64,'0'));
    const n=await P.add(pl.id, many);
    out.added=n;                                     // refused → 0
    out.stillEmpty=P.get(pl.id).tracks.length;       // and rolled back
    out.toldThem=(ctx.__toasts||[]).some(t=>/full/i.test(t));
  }

  if(CASE==='roundtrip'){
    await P.load();
    const pl=await P.create('Trip');
    const a='a'.repeat(64), b='b'.repeat(64), c='c'.repeat(64);
    await P.add(pl.id,[a,b,c,a]);                    // duplicate ignored
    out.tracks=P.get(pl.id).tracks.length;
    await P.move(pl.id,0,2);
    out.order=P.get(pl.id).tracks.map(t=>t[0]).join('');
    await P.removeTrack(pl.id,b);
    out.afterRemove=P.get(pl.id).tracks.map(t=>t[0]).join('');
    out.with_=P.playlistsWith(c).map(p=>p.name);
    await P.rename(pl.id,'Renamed');
    out.name=P.get(pl.id).name;
    // and what actually reached the relay is ENCRYPTED, tagged, and addressable
    const last=published.filter(e=>e.kind===30078).pop();
    out.encrypted = last.content.startsWith('E:');
    out.hasL = last.tags.some(t=>t[0]==='l'&&t[1]==='pcai-music');
    out.dTag = dOf(last).startsWith('pcai:playlist:');
    out.plaintextLeak = last.content.includes('Renamed');
  }

  process.stdout.write(JSON.stringify(out));
})();
"""


def run(case):
    with tempfile.TemporaryDirectory() as td:
        h = os.path.join(td, "h.js")
        open(h, "w", encoding="utf-8").write(HARNESS)
        r = subprocess.run(["node", h, JS, case], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


# ---- the pure part -----------------------------------------------------------------------------

def test_reorder_moves_and_clamps():
    o = run("reorder")
    assert o["mid"] == ["b", "c", "a", "d"]
    assert o["same"] == ["a", "b", "c"]
    assert o["clampHigh"] == ["b", "c", "a"]
    assert o["clampLow"] == ["c", "a", "b"]
    assert o["empty"] == []
    assert o["len"] == 3, "reordering must never lose or duplicate a track"


# ---- the corrections ---------------------------------------------------------------------------

def test_a_failed_resend_keeps_the_queued_edit():
    """Relay.publish resolves `{ok:false,msg:'timeout'}` — an OBJECT, which is truthy. Reading it as
    a boolean meant flush() counted a timeout as sent and emptied the queue, so an edit made offline
    was gone the next time the library was re-read from the network."""
    o = run("queue_keeps_failed_send")
    assert o["queued"] == 1, "a refused save must be held, not dropped"
    assert o["afterFailedFlush"] == 1, "a flush that did not land must keep the event"
    assert o["afterGoodFlush"] == 0, "…and clear it once it really is published"


def test_create_reports_a_save_that_did_not_happen():
    o = run("queue_keeps_failed_send")
    # Offline, the save is QUEUED, which is a kept edit — so create() hands the playlist back and
    # the user carries on. (`is False` here, not a tautology: a refusal that is not queued is what
    # must return null, and too_big below is that case.)
    assert o["createdWhileOffline"] is False, \
        "a queued save is not a failure — the playlist must still be usable offline"
    o2 = run("too_big")
    assert o2["added"] == 0 and o2["stillEmpty"] == 0, \
        "a refused add must roll back, not leave tracks the relay never got"
    assert o2["toldThem"], "and say why — a silent refusal is the bug this replaced"


def test_a_tombstone_is_not_undone_by_an_older_copy():
    """Deleting the map entry left the newer-wins guard nothing to compare against, so an older copy
    arriving later in the same batch — two relays disagreeing — resurrected the playlist, and the
    next edit republished it for good."""
    o = run("tombstone_wins")
    assert o["afterTombstone"] == [], "an older copy resurrected a deleted playlist"
    assert o["getIsNull"] is True
    assert o["afterNewer"] == ["Back"], "a genuinely newer edit must still win over the tombstone"


def test_the_kind5_is_published_before_the_tombstone():
    """The relay deletes every event for the address with created_at <= the kind-5's. Published
    after, it deletes the tombstone too — and a device that was offline then sees NOTHING for that
    d-tag, keeps its cached copy, and republishes it on the next edit."""
    o = run("delete_order")
    assert 5 in o["kinds"] and 30078 in o["kinds"]
    assert o["kind5Earlier"] is True, "the kind-5 must not out-rank the tombstone"
    assert o["tombstoneIsLast"] is True


def test_edits_round_trip_and_stay_encrypted():
    o = run("roundtrip")
    assert o["tracks"] == 3, "a duplicate add must be ignored"
    assert o["order"] == "bca", "move(0,2) did not reorder"
    assert o["afterRemove"] == "ca"
    assert o["with_"] == ["Trip"], "playlistsWith should find the playlist a track is on"
    assert o["name"] == "Renamed"
    assert o["encrypted"] and o["hasL"] and o["dTag"]
    assert o["plaintextLeak"] is False, "a playlist name must never reach the relay in the clear"


def test_an_offline_edit_is_kept_in_memory():
    o = run("rollback")
    assert o["made"] and o["oneTrack"] == 1
    assert o["addedWhileOffline"] == 1, "a queued edit counts as kept — it is not a failure"
    assert o["finalTracks"] == 2


# ---- the three registrations it cannot work without --------------------------------------------

def test_the_cache_pin_exempts_playlists():
    """The client cache evicts newest-N by created_at — right for the firehose, fatal for a document
    only its author can decrypt. Minutes of global-feed reading would drop the whole library off the
    device until a relay handed it back."""
    src = open(os.path.join(ROOT, "static", "js", "client", "store.js"), encoding="utf-8").read()
    fn = src[src.index("function _isPinned("):]
    fn = fn[:fn.index("\n  }")]
    assert "pcai:playlist" in fn, "playlists are not pinned — the firehose will evict them"


def test_it_is_loaded_and_precached():
    """A module in no <script> tag is `PCPlaylists is not defined`; one missing from the service
    worker's SHELL 404s on a cold offline start while every sibling works."""
    html = open(os.path.join(ROOT, "templates", "client.html"), encoding="utf-8").read()
    sw = open(os.path.join(ROOT, "static", "js", "client", "sw.js"), encoding="utf-8").read()
    assert "playlists.js" in html, "playlists.js is not loaded by the client shell"
    assert "'/static/js/client/playlists.js'" in sw, "playlists.js is not precached by the SW"


def test_the_queue_has_something_that_drains_it():
    src = open(JS, encoding="utf-8").read()
    assert re.search(r"addEventListener\('online'", src), \
        "nothing drains the offline queue, so a queued edit sits there for ever"
    assert "flush().catch" in src, "…and load() should drain whatever a previous session left"
