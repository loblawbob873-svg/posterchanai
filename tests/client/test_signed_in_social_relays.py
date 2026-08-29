"""Signed-in relay preferences must not disconnect Social from public discovery.

This was visible only on particular PCs because ``relaysEnabled`` is device-local.  A device with
custom/private relays replaced the managed pool and consequently saw only posts those relays held,
while logged-out users on the same deployment continued to see the public timeline.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = APP.index(f"function {name}(")
    brace = APP.index("{", start)
    depth = 0
    for pos in range(brace, len(APP)):
        if APP[pos] == "{":
            depth += 1
        elif APP[pos] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:pos + 1]
    raise AssertionError(f"function {name} does not close")


def _code(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", source)


def test_custom_relays_are_unionized_with_managed_discovery_relays():
    body = _code(_function("connectRelays"))
    custom = body.index("userRelays()")
    union = body.index("defaultRelays()", custom)
    configure = body.index("Relay.configure", union)
    assert custom < union < configure
    assert "[...list, ...defaultRelays().filter(Boolean)]" in body


def test_custom_relay_preference_is_not_overwritten_by_bootstrap():
    body = _code(_function("connectRelays"))
    assert "ClientSettings.set('relays'" not in body
    assert "ClientSettings.set('relaysEnabled'" not in body


def test_the_union_uses_the_normal_live_subscription_pool():
    connect = _code(_function("connectRelays"))
    timeline = _code(_function("renderTimeline"))
    assert "Relay.configure({ urls: list, verify: true })" in connect
    assert "Relay.subscribe(timelineFilter()" in timeline
    assert "subscribeFrom" not in timeline


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_custom_relay_session_receives_a_managed_relay_post_after_subscribing():
    """Drive shipped relay.js: the managed post is emitted only after Social is already live."""
    connect = _function("connectRelays")
    defaults = _function("defaultRelays")
    driver = r"""
class FakeWS {
  constructor(url){
    this.url=url; this.readyState=0; this.sent=[]; FakeWS.all.push(this);
    setTimeout(()=>{ this.readyState=1; if(this.onopen)this.onopen(); }, 0);
  }
  send(raw){ this.sent.push(JSON.parse(raw)); }
  close(){ this.readyState=3; }
  reply(msg){ if(this.onmessage)this.onmessage({data:JSON.stringify(msg)}); }
}
FakeWS.all=[];
global.WebSocket=FakeWS; global.window=global; global.self=global;
global.document={hidden:false,addEventListener(){}};
global.location={origin:'https://instance.test',protocol:'https:'};
global.navigator={onLine:true};
global.Worker=class {
  postMessage(msg){
    const events=(msg.args&&msg.args.events)||[];
    setTimeout(()=>this.onmessage&&this.onmessage({data:{id:msg.id,ok:true,
      data:events.map(e=>({id:e.id,valid:true}))}}),0);
  }
};
require(process.argv[1]);
const Relay=global.Relay;
const CFG={relay_url:'wss://managed.test'};
const FALLBACK_RELAYS=['wss://public.test/'];
const ClientSettings={get(k){ return k==='relaysEnabled'; }};
const userRelays=()=>['wss://private.test'];
const _dropLegacyAutoRelays=()=>false;
const defaultRelays=eval('('+JSON.parse(process.argv[2])+')');
const connectRelays=eval('('+JSON.parse(process.argv[3])+')');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  connectRelays(); await sleep(30);
  const received=[];
  // This is the network half installed by renderTimeline after its cache-first initial paint.
  const initialTimelineRendered=true;
  Relay.subscribe([{kinds:[1,6,1068,5,30023],limit:120}],
    {live:true,onEvent:ev=>received.push(ev.id)});
  await sleep(10);
  const managed=FakeWS.all.find(w=>w.url==='wss://managed.test');
  const req=managed && managed.sent.find(m=>m[0]==='REQ');
  if(!req) throw new Error('managed relay never received the timeline subscription');
  managed.reply(['EOSE',req[1]]);
  await sleep(10); // initial render/subscription is complete before the new post exists
  const ev={id:'a'.repeat(64),pubkey:'b'.repeat(64),sig:'c'.repeat(128),kind:1,
            created_at:Math.floor(Date.now()/1000),tags:[],content:'posted live'};
  managed.reply(['EVENT',req[1],ev]);
  await sleep(80); // relay.js signature-verification batch flush
  console.log(JSON.stringify({initialTimelineRendered,received,urls:Relay.urls()}));
  process.exit(0);
})().catch(e=>{console.error(e.stack||e);process.exit(1)});
"""
    proc = subprocess.run(
        ["node", "-e", driver, str(ROOT / "static/js/client/relay.js"),
         json.dumps(defaults), json.dumps(connect)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["initialTimelineRendered"] is True
    assert "wss://private.test" in out["urls"]
    assert "wss://managed.test" in out["urls"]
    assert out["received"] == ["a" * 64]
