"""Telegram never waits for ever (2026-10-22, the Windows app over Tor: "telegram is stuck at connecting...").
The node's log showed the app's last Telegram request at 12:11 and three live-socket redials in 30 s after the 12:07
restart, then nothing. Two waits had no limit: the app-session step before every request, and a live-socket handshake
stuck CONNECTING, which the guard in connect() treated as "already connected" for the rest of the session.
Loads the SHIPPED telegram.js in node, timers scaled 1000x down."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = r"""
const vm=require('vm'),fs=require('fs');
const fast=(fn,ms)=>setTimeout(fn,Math.max(0,(ms||0)/1000));
const sockets=[];
class WS{ constructor(u){ this.url=u; this.readyState=0; sockets.push(this); } close(){ this.readyState=3; } send(){} }
const win={addEventListener(){},screen:{width:1920,height:1080},innerWidth:1920,innerHeight:1080,
  localStorage:{getItem(){return null},setItem(){},removeItem(){}},matchMedia:()=>({matches:false,addEventListener(){}})};
win.__PC={ensureAiSession:()=>new Promise(()=>{}), authFetch:()=>new Promise(()=>{}), apiBase:()=>'https://x', aiToken:()=>''};
const ctx={window:win,document:{documentElement:{classList:{toggle(){},remove(){}}},addEventListener(){},querySelector:()=>null,getElementById:()=>null},
  localStorage:win.localStorage,setTimeout:fast,clearTimeout,console,FormData:class{},navigator:{},location:{href:'',origin:'https://x'},WebSocket:WS,screen:win.screen};
ctx.globalThis=ctx;vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const T=win.PCTelegram;
(async()=>{
  const out={};
  const t0=Date.now();
  try{ await T._api('/api/tgc/status'); out.session='resolved'; }catch(e){ out.session=e.message; out.sessionMs=Date.now()-t0; }
  T._state.status={state:'ready',configured:true};
  T._connect();
  await new Promise(r=>setTimeout(r,200));             // 20 s connect limit + 8 s redial, scaled
  out.sockets=sockets.length;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def _run():
    r = subprocess.run(["node", "-e", JS, str(ROOT / "static/js/client/telegram.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_sign_in_that_never_answers_ends_in_an_error_not_connecting_forever():
    got = _run()
    assert "sign-in did not answer" in got["session"], got


def test_a_live_socket_stuck_connecting_is_dropped_and_redialled():
    got = _run()
    assert got["sockets"] >= 2, f"a handshake stuck CONNECTING was never replaced: {got}"
