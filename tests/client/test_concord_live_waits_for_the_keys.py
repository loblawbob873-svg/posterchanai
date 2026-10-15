"""A room's live subscription waits for its keys -- it is not "failed" for ten minutes because it ran first.

"more concord plane key errors wtf" / "laptop concord behind, not updating room" (2026-10-15). startRoomsLive
runs every live tick from the first one, usually before a room's control envelopes are back. With no
controls every stream author throws "Concord plane key is not held by this membership"; that was recorded
as a FAILURE and a failure is retried only after ROOM_LIVE_RETRY_MS (10 min), so keys arriving a second
later changed nothing and the room took no live messages. The SHIPPED loop is run under node: first tick
with no keys, then the keys arrive, then the next tick.
"""
from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _run():
    body = SRC[SRC.index("  const roomsLiveFailed=new Map()"):]
    body = body[:body.index("\n  let memberQuery=")]
    script = """
const warns=[], subs=[];
console.warn=(...a)=>warns.push(a.join(' '));
const roomsLive=new Map(), roomControls=new Map();
const room={communityId:'c1',naddr:'n1',cord:{bundle:{}},channels:[{id:'ch',name:'general',streamPubkeys:['aa']}]};
const saved=()=>[room], channelsOf=r=>r.channels, roomRelays=()=>['wss://r'], roomIdentity=r=>r.communityId;
const deliveryOwner=()=>'me', cordControlStamp=w=>[...new Set((w||[]).map(e=>e.id))].sort();
const cordPlaneContext=(p,b,controls)=>({controls});
const cordPlaneSubscribe=(p,R,relays,filters,opts,plane)=>{
  if(!plane.controls.length)throw new Error('Concord plane key is not held by this membership');
  subs.push(plane.controls.map(w=>w.id)); const s=()=>{}; return s; };
const cachedEnvelopes=async()=>null, envelopeCacheKey=(a,b)=>a+':'+b;
globalThis.window={PosterCordReader:{createPlaneAuth(){}},PCConcordRelay:{subscribeFrom(){}}};
globalThis.document={body:{classList:{contains:()=>false}}};
BODY
(async()=>{
  startRoomsLive({});                       // tick 1: the keys are not back yet
  await new Promise(r=>setTimeout(r,20));
  roomControls.set('c1',[{id:'k1'}]);       // the room's controls arrive
  startRoomsLive({});                       // the next tick, seconds later
  console.log(JSON.stringify({warns,subs}));
})();
""".replace("BODY", body)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_room_subscribes_as_soon_as_its_keys_arrive():
    got = _run()
    assert got["subs"] == [["k1"]], ("the room took no live subscription after its keys arrived", got)


def test_no_plane_key_error_is_logged_for_a_room_still_loading():
    got = _run()
    assert not [w for w in got["warns"] if "plane key" in w], got
