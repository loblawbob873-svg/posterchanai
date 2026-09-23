"""Sharing Music with other people — the shipped musicshare.js, RUN for three people.

`music_share_runtime.mjs` drives the real module with real NIP-44 (nostr-tools) and real WebCrypto
against one in-memory relay and one Blossom that keeps per-blob OWNERS the way the real server does,
and reads B's added track back through app.js's OWN `_driveDecrypt`. Each scenario below is a way
this feature loses or leaks something silently:

  * the one-track key must never be (or reveal) the drive's master key;
  * C — a stranger — must neither SEE the share nor be able to open it when handed the event;
  * a recipient's added copy must outlive the sharer stopping the share AND releasing their copy;
  * sharing the same song twice must be ONE stored blob (deterministic key + content IV);
  * revoking one share must not release a copy another live share still points at;
  * a read that did not complete must never release anything (the replaceable-doc-wipe shape);
  * a forged body, or a document addressed to someone else, is not a share with me;
  * two shares made at once must not interleave their writes.

Then the registrations it cannot work without — each one has been missed by an earlier feature.
"""
import json
import os
import re
import shutil
import subprocess

import pytest
from tests.client_source import client_source

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUNTIME = os.path.join(HERE, "music_share_runtime.mjs")


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def results():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=300)
    assert r.stdout.strip(), f"the runtime printed nothing:\n{r.stderr[-2000:]}"
    return json.loads(r.stdout)


SCENARIOS = [
    "derived key is per track, deterministic, and not the master key",
    "wire validation refuses junk",
    "A shares; B plays it; B adds it; A stops sharing; B keeps it; C never could",
    "the same song shared twice is ONE blob",
    "revoking one of two shares keeps the copy the other still uses",
    "an incomplete read never releases anything",
    "refIds names every copy a live share uses",
    "a share too big for one event carries its list in a sealed blob",
    "a forged body naming another author is ignored",
    "a document addressed to somebody else is not a share with me",
    "writes are serialized",
    "adding twice does not duplicate, and uploads nothing the second time",
    "a release skipped by an incomplete read happens on the next complete one",
    "stopping with one of two people keeps the copy; the last one releases it",
    "a release that fails is retried, not forgotten",
    "a big share's sealed song list and its songs are all released",
    "sharing again after stopping works and plays",
    "a copy shared again before the retry is not released",
]


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario(results, name):
    assert name in results, f"scenario missing: {name!r} (have {list(results)})"
    row = results[name]
    assert row["ok"], f"{name}: {json.dumps(row['detail'])[:3000]}"


# ---- registrations --------------------------------------------------------------------------------

def test_loaded_and_precached():
    """A module in no <script> tag is `PCMusicShare is not defined`; one missing from the service
    worker's shell 404s on a cold offline start while every sibling works."""
    # Loaded ON DEMAND (it put the boot payload over budget) — so it must NOT be a boot <script>,
    # app.js must know how to load it, and the SW must still precache it for offline starts.
    assert "musicshare.js" not in _read("templates", "client.html")
    assert "_loadScript('/static/js/client/musicshare.js'" in client_source()
    assert "'/static/js/client/musicshare.js'" in _read("static", "js", "client", "sw.js")


def test_carried_on_a_relay_change():
    """The share documents live only on relays: left on the old pool, the recipient's list goes too."""
    s = client_source()
    carry = s[s.index("const _CARRY_D = ["):]
    carry = carry[:carry.index("];")]
    assert "pcai:musicshare:" in carry


def test_own_shares_are_pinned_and_strangers_are_not():
    """RUN store.js: a share I wrote survives a firehose; one addressed to me by anybody else is an
    ordinary cache entry (pinning those would let a stranger mint unevictable entries)."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    store = os.path.join(ROOT, "static", "js", "client", "store.js")
    js = """
      const fs=require('fs'), vm=require('vm');
      const ctx={console,setTimeout,clearTimeout,setInterval,clearInterval,indexedDB:undefined,
        crypto:require('crypto').webcrypto,navigator:{onLine:true},
        localStorage:{_d:{},getItem(k){return this._d[k]||null},setItem(k,v){this._d[k]=String(v)},removeItem(k){delete this._d[k]}}};
      ctx.window=ctx; ctx.self=ctx; ctx.globalThis=ctx; vm.createContext(ctx);
      vm.runInContext(fs.readFileSync(process.argv[1],'utf8'), ctx);
      const S=ctx.window.Store, ME='a'.repeat(64), THEM='b'.repeat(64);
      if(S.setViewer) S.setViewer(ME);
      const doc=(pk,i)=>({id:pk[0]+'s'+i,pubkey:pk,kind:30078,created_at:1000+i,content:'x',sig:'x',
        tags:[['d','pcai:musicshare:abcd'+i+':'+(pk===ME?THEM:ME).slice(0,16)],['p',pk===ME?THEM:ME],['l','pcai-musicshare']]});
      for(let i=0;i<50;i++){ S.saveEvent(doc(ME,i)); S.saveEvent(doc(THEM,i)); }
      for(let i=0;i<9000;i++) S.saveEvent({id:'p'+i,pubkey:'c'.repeat(64),kind:1,created_at:9000000+i,tags:[],content:'hi',sig:'x'});
      const q=pk=>S.query([{authors:[pk],kinds:[30078],'#l':['pcai-musicshare'],limit:5000}]).length;
      process.stdout.write(JSON.stringify({mine:q(ME),theirs:q(THEM)}));
    """
    r = subprocess.run(["node", "-e", js, store], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert got["mine"] == 50, f"the firehose evicted shares this account made: {got}"
    assert got["theirs"] < 50, f"a stranger's share documents were pinned: {got}"


def test_the_drive_reclaim_counts_shared_copies_as_referenced():
    """RUN app.js's `_syncRefIds` — the reference set the drive check's reclaim is computed against.
    The shared copies are keep-flagged and named by no drive entry, i.e. exactly the reclaim set, so
    they must be IN it; and a share list that could not be read must make the whole set null (no
    offer), the same rule an unreadable synced folder follows."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    app = os.path.join(ROOT, "static", "js", "client", "app.js")
    js = r"""
      const fs=require('fs'), vm=require('vm'), path=require('path');
      // app.js plus the modules split out of it (_syncRefIds lives in files.js), read from beside it.
      const cs=require(path.resolve(path.dirname(process.argv[1]),'../../../tests/client/client_source.cjs'));
      const src=cs.clientSourceAt(process.argv[1]);
      const at=src.indexOf('async function _syncRefIds(');
      let i=src.indexOf('{',at), d=0; for(;i<src.length;i++){ if(src[i]==='{')d++; else if(src[i]==='}'&&--d===0)break; }
      const fn=src.slice(at,i+1);
      const run=async(shared, loads=true)=>{
        const ctx={ _musicShareLoad: async()=> loads ? ctx.window.PCMusicShare : null, window:{ PCSync:{ acct:()=>[{key:'f'}], accountFolders:async()=>{},
                                      docs:{ state:async()=>({state:{'a.txt':{sha:'s1'}}}) } },
                             PCMusicShare:{ refIds:async()=>shared } } };
        vm.createContext(cs.installStateGlobals(ctx) && ctx); vm.runInContext(fn+';this.f=_syncRefIds;', ctx);
        const r=await ctx.f(); return r ? [...r].sort() : null; };
      (async()=>{ process.stdout.write(JSON.stringify({
        ok: await run(new Set(['copy1','list1'])), unreadable: await run(null),
        noModule: await run(new Set(['copy1']), false) })); })();
    """
    r = subprocess.run(["node", "-e", js, app], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert got["ok"] == ["copy1", "list1", "s1"], got
    assert got["unreadable"] is None, "an unreadable share list must kill the reclaim offer"
    assert got["noModule"] is None, "if sharing cannot be loaded the reclaim must not be offered at all"


def test_player_reads_titles_through_one_lookup():
    """A shared track the player is playing has no drive-index record; every place that names the
    current track must ask `_trackMeta`, or the lock screen/widget says '—' over a playing song."""
    s = client_source()
    mp = s[s.index("  const MusicPlayer = {"):]
    mp = mp[:mp.index("\n  };")]
    assert "FilesIdx.meta(this.cur)" not in mp and "FilesIdx.meta(sha)" not in mp


# ---- accepting a share, and what it becomes -------------------------------------------------------

def test_an_accepted_share_is_an_ordinary_playlist_chip():
    """"Playlists that are shared with you should just appear as a regular playlist for simplicity.
    The Shared With Me button should be you accepting or rejecting the share."

    A share used to be a place you visited: an inbox of cards, each opening a screen of its own with
    its own back button. So the bar is asked directly — an unanswered share is NOT a playlist, an
    accepted one IS one (with its track count, like any playlist), and the inbox chip counts only
    what is still waiting."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    src = _read("static", "js", "client", "musicshare.js")
    js = """
      const fs=require('fs'), vm=require('vm');
      const store={};
      const ctx={console, localStorage:{getItem:k=>store[k]||null, setItem:(k,v)=>{store[k]=String(v)}, removeItem:k=>{delete store[k]}},
        setTimeout, clearTimeout, Date, Math, JSON, Object, Array, Set, Map, Number, String, Boolean, Promise,
        // cleanItem decodes every key with atob; without it each track is 'invalid' and a share is empty.
        atob:s=>Buffer.from(String(s),'base64').toString('binary'), btoa:s=>Buffer.from(String(s),'binary').toString('base64'),
        Uint8Array, Buffer};
      ctx.window=ctx; ctx.globalThis=ctx; ctx.self=ctx;
      ctx.__PC={ me:()=>({pubkey:'a'.repeat(64)}), nip44enc:async()=>'ct', nip44dec:async()=>'{}',
                 publish:async()=>({ok:true}) };
      // The module reads window.Relay / window.Store, so that is where the fakes go.
      ctx.Relay={ready:async()=>{}, query:async()=>[]}; ctx.Store={query:()=>[], saveEvent(){}};
      vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1],'utf8'), ctx);
      const MS=ctx.window.PCMusicShare;
      const ME='a'.repeat(64), THEM='b'.repeat(64);
      // Two offers, delivered the way the relay delivers them — through the module's own loadIn,
      // so the shapes here are the shipped ones rather than a fixture's idea of them.
      const body=(id,name,n)=>({v:1,id,name,from:THEM,srv:'',created:1,updated:1,
        tracks:Array.from({length:n},(_,i)=>({s:String(i).padStart(64,'0'),k:'A'.repeat(43)+'=',iv:'b'.repeat(16),n:'t'+i,m:'audio/mpeg',z:1,e:'mp3'})),tl:null});
      const bodies={trip:body('trip','Road trip',2), study:body('study','Study',5)};
      const ev=id=>({id:id+'x', pubkey:THEM, kind:30078, created_at:10, content:id,
        tags:[['d','pcai:musicshare:'+id+':'+ME.slice(0,16)],['p',ME],['l','pcai-musicshare']]});
      ctx.Store={query:()=>[ev('trip'),ev('study')], saveEvent(){}};
      ctx.__PC.nip44dec=async (pk,ct)=>JSON.stringify(bodies[ct]);
      const out={};
      (async()=>{
        await MS.loadIn();
        const KEY=id=>THEM+':'+id;
        out.waitingBar=MS.barHTML('', false);
        MS.decide(KEY('trip'), true);
        out.acceptedBar=MS.barHTML('', false);
        out.isView=[MS.isView(KEY('trip')), MS.isView(KEY('study')), MS.isView('__shared_in')];
        out.accepted=MS.acceptedShares().map(s=>s.body.name);
        out.pending=MS.pendingShares().map(s=>s.body.name);
        MS.decide(KEY('study'), false);
        out.afterReject={accepted:MS.acceptedShares().length, pending:MS.pendingShares().length};
        out.decisions=Object.keys(MS.decisions()).length;
        process.stdout.write(JSON.stringify(out));
      })().catch(e=>{ console.error(e); process.exit(1); });
    """
    path = os.path.join(ROOT, "static", "js", "client", "musicshare.js")
    r = subprocess.run(["node", "-e", js, path], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert "Road trip" not in got["waitingBar"], "an unanswered share is already a playlist chip"
    assert 'ma-pln">2<' in got["waitingBar"], "the inbox chip does not count the shares waiting"
    assert "Road trip" in got["acceptedBar"] and 'ma-pln">2<' in got["acceptedBar"], \
        f"an accepted share is not a playlist chip with its track count: {got['acceptedBar'][:200]}"
    assert got["isView"] == [True, False, True], "an accepted share must route to its own renderer"
    assert got["accepted"] == ["Road trip"] and got["pending"] == ["Study"]
    assert got["afterReject"] == {"accepted": 1, "pending": 0}, "a rejected share came back"
    assert got["decisions"] == 2


def test_the_decisions_follow_the_account():
    """localStorage alone is how auto-mute came back on for people who had turned it off elsewhere.
    Accepting a playlist on the phone and not having it on the desktop is the same failure, so the
    answers live in a private per-account document — which means the two registrations every such
    document here has needed at least once."""
    assert "'pcai:musicshares'" in _read("static", "js", "client", "store.js"), \
        "not pinned: a firehose evicts it and every answered offer is offered again"
    carry = _read("static", "js", "client", "app.js")
    carry = carry[carry.index("const _CARRY_D = ["):]
    assert "pcai:musicshares" in carry[:carry.index("];")], \
        "not carried: left on the old relay pool, the playlists you kept disappear"


def test_it_never_publishes_over_a_document_it_has_not_read():
    """The replaceable-doc wipe: an unreachable relay plus a fresh device would otherwise replace
    every answer this account has ever given with the one just made here."""
    src = _read("static", "js", "client", "musicshare.js")
    save = src[src.index("async function _saveDecisions(){"):]
    save = save[:save.index("\n  }")]
    assert "if(!_decRead)" in save, "a save with no prior read is a wipe"


def _run_decisions_js(body):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = """
      const fs=require('fs'), vm=require('vm');
      const SRC=fs.readFileSync(process.argv[1],'utf8');
      const store={};   // ONE device: localStorage outlives the reload an account switch does
      const A='a'.repeat(64), B='b'.repeat(64), C='c'.repeat(64);
      function boot(me, relayEvs){
        const ctx={console, localStorage:{getItem:k=>store[k]||null, setItem:(k,v)=>{store[k]=String(v)}, removeItem:k=>{delete store[k]}},
          setTimeout, clearTimeout, Date, Math, JSON, Object, Array, Set, Map, Number, String, Boolean, Promise, Uint8Array, Buffer,
          atob:s=>Buffer.from(String(s),'base64').toString('binary'), btoa:s=>Buffer.from(String(s),'binary').toString('base64')};
        ctx.window=ctx; ctx.globalThis=ctx; ctx.self=ctx;
        ctx.published=[];
        ctx.__PC={ me:()=>({pubkey:me}),
          nip44enc:async (pk,pt)=>'enc:'+pk+':'+pt,
          nip44dec:async (pk,ct)=>{ const p='enc:'+pk+':'; if(!String(ct).startsWith(p)) throw new Error('bad mac'); return ct.slice(p.length); },
          publish:async (k,ct,tags)=>{ ctx.published.push({k,ct,tags}); return {ok:true}; } };
        ctx.Relay={ready:async()=>{}, query:async()=>relayEvs||[]}; ctx.Store={query:()=>[], saveEvent(){}};
        vm.createContext(ctx); vm.runInContext(SRC, ctx);
        ctx.MS=ctx.window.PCMusicShare; return ctx;
      }
      (async()=>{ const out={}; """ + body + """ process.stdout.write(JSON.stringify(out)); })()
        .catch(e=>{ console.error(e); process.exit(1); });
    """
    path = os.path.join(ROOT, "static", "js", "client", "musicshare.js")
    r = subprocess.run(["node", "-e", js, path], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def test_decisions_do_not_cross_accounts_on_one_device():
    """An account switch reloads the page but keeps localStorage. With one device-wide key, B's first
    load merged A's answers — who shared music with A, and what A kept or refused — into B's OWN
    relay document, and A's "reject" hid the same share (one body id goes to every recipient) from B."""
    got = _run_decisions_js("""
      const a=boot(A, []);
      await a.MS.loadDecisions();
      a.MS.decide(C+':trip', false);
      await new Promise(r=>setTimeout(r,20));
      const b=boot(B, []);                     // the switch: same storage, a fresh page
      await b.MS.loadDecisions();
      out.bSees=Object.keys(b.MS.decisions());
      b.MS.decide(C+':other', true);
      await new Promise(r=>setTimeout(r,20));
      out.bPublished=b.published.map(p=>p.ct).join('|');
      const a2=boot(A, []);
      await a2.MS.loadDecisions();
      out.aStill=Object.keys(a2.MS.decisions());
    """)
    assert got["bSees"] == [], f"account B inherited account A's answers: {got['bSees']}"
    assert "trip" not in got["bPublished"], "account A's answers were published into B's document"
    assert got["aStill"] == ["c" * 64 + ":trip"], got


def test_a_foreign_decisions_document_cannot_shadow_the_owners():
    """A document with the same `d` from ANOTHER pubkey (a relay that ignores `authors`) could not be
    decrypted anyway — but being newer, it was picked over the owner's own, the decrypt failed, and
    the account's answers read as never given."""
    got = _run_decisions_js("""
      const own={id:'o', pubkey:A, kind:30078, created_at:100, tags:[['d','pcai:musicshares']],
                 content:'enc:'+A+':'+JSON.stringify({v:1,d:{[C+':trip']:{yes:true,at:50}}})};
      const forged={id:'f', pubkey:C, kind:30078, created_at:9999999999, tags:[['d','pcai:musicshares']],
                 content:'enc:'+C+':'+JSON.stringify({v:1,d:{}})};
      const a=boot(A, [own, forged]);
      await a.MS.loadDecisions();
      out.keys=Object.keys(a.MS.decisions());
    """)
    assert got["keys"] == ["c" * 64 + ":trip"], got
