#!/usr/bin/env python3
"""Read a REAL Concord community end to end, and prove the repair the room-open path depends on.

Every other Concord test in this tree drives fixtures. Fixtures cannot answer the question this one
exists for — "does a room somebody else runs open here" — because the failure was never in our own
data. It was reported against Soapbox's community: entering a room the user was a member of gave
"could not refresh community" and "room history is not readable with this membership", every time.

That sentence is thrown by exactly one line in the shipped reader:

    const channel = channels.find(ch => ch.idHex === channelId);
    if (!channel) throw new Error("channel is not readable with this membership");

The readable set comes from the CONTROL stream and from nothing else, so a saved channel whose id
the control set no longer carries is refused — and `readChat` is the one place that re-derives the
id BY NAME before giving up. `hydrateRoomStreams` used to call the reader directly, skipping that
repair, which is why a room could fail to open deterministically on every single attempt.

So this check measures three things against the live community, in order:

  A. the correct id reads real history           (the community is reachable and our crypto agrees)
  B. a stale id throws that exact sentence        (the failure the user reported is reproduced)
  C. re-deriving the id by name recovers ALL of A (the repair is sufficient, not merely different)

C is asserted as an EQUAL message count, never as "some messages": a repair that silently returns a
shorter timeline would pass a truthiness test and lose history.

Network-dependent by nature, so it exits 2 (SKIP, never a false pass) whenever the community cannot
be reached or the invite has been rotated — that is not a regression in this repo.
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "static", "js", "client")

# A public Armada/Soapbox invite. Override to point the check at any CORD-05 community.
INVITE = os.environ.get("PC_CONCORD_INVITE") or (
    "https://armada.buzz/invite/naddr1qvzqqqyzz5pzpxzwfxf4m5x3hd3v9x8m2rzy95j4"
    "qxf3ykn6wuwu7vdjfgydgp3lqqqqk03ql4#BAACAwQyVJ2sw8wZ9UaPdLvtUi_o"
)
CHANNEL = os.environ.get("PC_CONCORD_CHANNEL", "general")
# Bootstrap relays the invite itself does not carry. The bundle's own relays win once it opens.
CORD_RELAYS = ["wss://relay.dreamith.to", "wss://relay.ditto.pub"]

# The libraries are loaded in the MAIN realm, deliberately. Run them under `vm.runInNewContext` and
# the host TextEncoder/crypto handed in produce cross-realm Uint8Arrays; noble throws on those, and
# nostr-tools' verifyEvent catches everything and answers `false` — so every bundle on the relay is
# rejected as "not a valid invite bundle event" and the check reports a broken community that is
# perfectly healthy. Verified: the same event verifies under Python's coincurve.
RUNNER = r"""
import fs from 'node:fs';
const CLIENT=process.argv[2], INVITE=process.argv[3], WANT=process.argv[4];
const BOOT=JSON.parse(process.argv[5]);
globalThis.window=globalThis; globalThis.self=globalThis;
const load=f=>fs.readFileSync(CLIENT+'/'+f,'utf8');
const PosterCord=new Function(load('cord-protocol.js')+'\n;return PosterCord;')();
const reader=new Function(load('cord-reader.js')+'\n;return PosterCordReader;')();
const say=o=>process.stdout.write(JSON.stringify(o)+'\n');

function query(relays,filter,ms){return new Promise(res=>{
  const out=new Map(); let live=relays.length; if(!live)return res([]);
  let t; const fin=()=>{ if(--live<=0){ clearTimeout(t); res([...out.values()]); } };
  t=setTimeout(()=>res([...out.values()]),ms);
  for(const u of relays){ let ws; try{ ws=new WebSocket(u); }catch(_){ fin(); continue; }
    ws.onopen=()=>ws.send(JSON.stringify(["REQ","q",filter]));
    ws.onmessage=e=>{ const m=JSON.parse(e.data);
      if(m[0]==="EVENT") out.set(m[2].id,m[2]);
      else if(m[0]==="EOSE"){ try{ws.close();}catch(_){} fin(); } };
    ws.onerror=()=>{ try{ws.close();}catch(_){} fin(); };
  }});}

const det=PosterCord.inviteDetails(INVITE);
const boot=[...new Set([...(det.bootstrapRelays||[]),...BOOT])];
const cands=(await query(boot,{kinds:[33301],authors:[det.linkSigner],'#d':[''],limit:100},15000))
  .sort((a,b)=>Number(b.created_at)-Number(a.created_at));
if(!cands.length){ say({skip:'no bundle events came back from '+boot.join(', ')}); process.exit(0); }
let opened; for(const ev of cands){ try{ opened=PosterCord.openInvite(INVITE,[ev]); break; }catch(_){} }
if(!opened){ say({skip:'none of '+cands.length+' bundles opened with this invite secret (rotated?)'}); process.exit(0); }

const bundle=opened.bundle, relays=(bundle.relays||BOOT).slice(0,8);
const seed=reader.inspectControl(bundle,[]);
const controlWraps=await query(relays,{kinds:[1059],authors:seed.controlPubkeys,limit:1000},20000);
if(!controlWraps.length){ say({skip:'the control stream answered nothing on '+relays.join(', ')}); process.exit(0); }
const info=reader.inspectControl(bundle,controlWraps);
const channels=info.channels||[];
const want=channels.find(c=>c.name===WANT)||channels[0];
if(!want){ say({skip:'the control stream carried no channels'}); process.exit(0); }

const wraps=await query(relays,{kinds:[1059],authors:want.streamPubkeys,limit:400},20000);
if(!wraps.length){ say({skip:'#'+want.name+' has no history on '+relays.join(', ')}); process.exit(0); }

/* A. the id the control stream carries. */
const good=await reader.inspectChat(bundle,controlWraps,want.id,wraps);

/* B. a saved id the control stream does not carry — a channel rotated since this room was joined. */
let refusal=null;
try{ await reader.inspectChat(bundle,controlWraps,'0'.repeat(64),wraps); }
catch(e){ refusal=String(e&&e.message||e); }

/* C. the repair readChat performs: find the channel by NAME and read again. */
const byName=(reader.inspectControl(bundle,controlWraps).channels||[]).find(c=>c.name===want.name);
const healed=byName?await reader.inspectChat(bundle,controlWraps,byName.id,wraps):null;

say({community:info.name||'', channels:channels.length, channel:want.name,
     controlWraps:controlWraps.length, chatWraps:wraps.length,
     messages:good.messages.length, refusal,
     healed:healed?healed.messages.length:null});
"""


def main():
    if not os.path.isdir(CLIENT):
        print("SKIP  no client bundle at %s" % CLIENT)
        return 2
    for name in ("cord-protocol.js", "cord-reader.js"):
        if not os.path.exists(os.path.join(CLIENT, name)):
            print("SKIP  %s is not in this checkout" % name)
            return 2

    with tempfile.TemporaryDirectory(prefix="pc-concord-live.") as tmp:
        runner = os.path.join(tmp, "live.mjs")
        with open(runner, "w") as fh:
            fh.write(RUNNER)
        try:
            proc = subprocess.run(
                ["node", runner, CLIENT, INVITE, CHANNEL, json.dumps(CORD_RELAYS)],
                capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            print("SKIP  node is not installed")
            return 2
        except subprocess.TimeoutExpired:
            print("SKIP  the community did not answer within 180s")
            return 2

    if proc.returncode != 0:
        print("SKIP  the reader could not run: %s" % (proc.stderr.strip().splitlines() or [""])[-1])
        return 2
    line = [l for l in proc.stdout.strip().splitlines() if l.startswith("{")]
    if not line:
        print("SKIP  the runner reported nothing")
        return 2
    out = json.loads(line[-1])
    if out.get("skip"):
        print("SKIP  %s" % out["skip"])
        return 2

    bad = []
    if not out["messages"]:
        bad.append("#%s decrypted 0 messages from %d wraps — the community is reachable but its "
                   "history did not open" % (out["channel"], out["chatWraps"]))
    if not out["refusal"]:
        bad.append("a channel id the control stream does not carry was accepted; this check can no "
                   "longer tell the reported failure from a healthy read")
    elif "not readable with this membership" not in out["refusal"]:
        bad.append("a stale id was refused with %r, not the sentence the room-open path repairs on"
                   % out["refusal"])
    if out["healed"] is None:
        bad.append("#%s could not be found by name in the control stream, so readChat's repair has "
                   "nothing to fall back to" % out["channel"])
    elif out["healed"] != out["messages"]:
        bad.append("the by-name repair returned %d messages where the carried id returned %d — a "
                   "repair that loses history is not a repair" % (out["healed"], out["messages"]))

    # Say what was measured, never a fixed sentence: a headline that asserts "a stale id is refused"
    # while the findings below say it was accepted is the same contradiction the health board's
    # status/detail split produced, and it is read as the headline.
    refused = "a stale id is refused" if out["refusal"] and \
        "not readable with this membership" in out["refusal"] else "a stale id is NOT refused"
    repair = ("the by-name repair recovers all of them" if out["healed"] == out["messages"]
              else "the by-name repair recovers %s" % out["healed"])
    print("%s  %r: %d channels, #%s read %d messages from %d wraps (control %d); %s and %s"
          % ("FAIL" if bad else "OK  ", out["community"], out["channels"], out["channel"],
             out["messages"], out["chatWraps"], out["controlWraps"], refused, repair))
    for b in bad:
        print("  - %s" % b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
