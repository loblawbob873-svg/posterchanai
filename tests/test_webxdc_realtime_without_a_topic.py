"""An Armada game whose session id is a UUID gets a QUIET realtime channel in a Concord room, never an error.

Reported 2026-10-15 on Josephus's DC20 Dice: "realtime channel could not start: attachment has no interoperable
webxdc topic (joinRealtimeChannel)". His post carries `webxdc ccede1cf-…` -- an older Armada session UUID, not a
52-char base32 Iroh topic. Armada joins gossip ONLY for a real topic (useConcordAppSync: `isTopicId(uuid)`) and
otherwise lets the game run with a realtime channel that carries nothing -- "no relay fallback: it would split the
room". The shipped Session is driven through its own `webxdc.rtJoin` RPC:

  UUID session    -> the join SUCCEEDS, Iroh is never dialled, and a send goes nowhere (no relay fallback)
  real topic      -> Iroh is joined exactly as before
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEBXDC = ROOT / "static/js/client/webxdc.js"
IROH = ROOT / "static/js/client/webxdc-iroh.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _join(uuid, webxdc=WEBXDC):
    script = r"""
global.crypto = require('node:crypto').webcrypto;
const published = [], joined = [];
global.window = { addEventListener(){}, removeEventListener(){},
  NostrTools:{ generateSecretKey:()=>Uint8Array.of(1), getPublicKey:()=>'rt', finalizeEvent:e=>e },
  Relay:{ publishFastAll(){ published.push('relay'); return 1; }, subscribe(){ return 'sub'; }, ready:async()=>{}, close(){} },
  __PC:{ $(){}, enc:s=>s, toast(){}, me:()=>({pubkey:'me'}), profOf:()=>({}), apiBase:()=>'https://fixture.invalid' } };
global.document = { addEventListener(){}, removeEventListener(){}, querySelectorAll:()=>[] };
global.location = { hostname:'fixture.invalid', href:'https://fixture.invalid/' };
require(%(iroh)s);
const realDecode = window.PCWebxdcIroh.decodeTopic;
global.PCWebxdcIroh = window.PCWebxdcIroh = { decodeTopic:realDecode, join:async (topic)=>{ const b = realDecode(topic); if(!b || b.length !== 32) throw new Error('attachment has no interoperable Webxdc topic'); /* the shipped join's first check */ joined.push(topic); return { send(){ published.push('iroh'); }, leave(){} }; } };
global.PCConcord = window.PCConcord = {
  webxdcPublish(){ published.push('concord-relay'); return Promise.resolve(); },
  webxdcSubscribe(){ published.push('concord-subscribe'); return Promise.resolve(()=>{}); },
};
require(%(webxdc)s);
const transport = { protocol:'concord2', room:'room', channel:'general', channelId:'c1' };
const s = new window.PCWebxdc.Session({ uuid:%(uuid)s, url:'https://fixture.invalid/dice.xdc', transport },
                                      { index:new Map(), bytes:new Uint8Array() });
const answers = [];
s.reply = (id, r) => answers.push({ id, ok:true });
s.fail = (id, m) => answers.push({ id, ok:false, m });
s.post = () => {};
(async () => {
  s.onRpc({ id:1, method:'webxdc.rtJoin', params:{} });
  for(let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));
  s.rtSend('AAAA');
  for(let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({ answers, joined, published }));
})().catch(e => { console.error(e); process.exitCode = 1; });
""" % {"iroh": json.dumps(str(IROH)), "webxdc": json.dumps(str(webxdc)), "uuid": json.dumps(uuid)}
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_uuid_session_gets_a_quiet_channel_not_an_error():
    got = _join("ccede1cf-73a4-4e26-a54c-147033e64d2c")
    assert got["answers"] == [{"id": 1, "ok": True}], ("joinRealtimeChannel failed for an Armada UUID session", got)
    assert got["joined"] == [], got
    assert got["published"] == [], ("a quiet channel must not fall back to a relay -- that splits the room", got)


def test_a_real_topic_still_joins_iroh():
    topic = "FBSTBOCHXLXTUPWPCLPAGBZDFNBSK5HKMTTMTVAKHY2EGNJFYMLQ"
    got = _join(topic)
    assert got["answers"] == [{"id": 1, "ok": True}] and got["joined"] == [topic], got
    assert got["published"] == ["iroh"], got
