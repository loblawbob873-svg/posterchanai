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
    assert "musicshare.js" in _read("templates", "client.html")
    assert "'/static/js/client/musicshare.js'" in _read("static", "js", "client", "sw.js")


def test_carried_on_a_relay_change():
    """The share documents live only on relays: left on the old pool, the recipient's list goes too."""
    s = _read("static", "js", "client", "app.js")
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
      const fs=require('fs'), vm=require('vm');
      const src=fs.readFileSync(process.argv[1],'utf8');
      const at=src.indexOf('async function _syncRefIds(');
      let i=src.indexOf('{',at), d=0; for(;i<src.length;i++){ if(src[i]==='{')d++; else if(src[i]==='}'&&--d===0)break; }
      const fn=src.slice(at,i+1);
      const run=async(shared)=>{
        const ctx={ window:{ PCSync:{ acct:()=>[{key:'f'}], accountFolders:async()=>{},
                                      docs:{ state:async()=>({state:{'a.txt':{sha:'s1'}}}) } },
                             PCMusicShare:{ refIds:async()=>shared } } };
        vm.createContext(ctx); vm.runInContext(fn+';this.f=_syncRefIds;', ctx);
        const r=await ctx.f(); return r ? [...r].sort() : null; };
      (async()=>{ process.stdout.write(JSON.stringify({
        ok: await run(new Set(['copy1','list1'])), unreadable: await run(null) })); })();
    """
    r = subprocess.run(["node", "-e", js, app], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert got["ok"] == ["copy1", "list1", "s1"], got
    assert got["unreadable"] is None, "an unreadable share list must kill the reclaim offer"


def test_player_reads_titles_through_one_lookup():
    """A shared track the player is playing has no drive-index record; every place that names the
    current track must ask `_trackMeta`, or the lock screen/widget says '—' over a playing song."""
    s = _read("static", "js", "client", "app.js")
    mp = s[s.index("  const MusicPlayer = {"):]
    mp = mp[:mp.index("\n  };")]
    assert "FilesIdx.meta(this.cur)" not in mp and "FilesIdx.meta(sha)" not in mp
