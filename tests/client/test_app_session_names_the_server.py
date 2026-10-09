"""An app session that cannot reach the instance says WHICH server, and for a .onion says what is needed
(Orbot in VPN mode with PosterChan in its app list). Reported: a phone on our .onion got "could not establish
your app session: failed to fetch" — indistinguishable from a dead server. Runs the SHIPPED ensureAiSession
under node with fetch failing the way a WebView fails when the onion name cannot resolve."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()


def _slice(start, end_marker):
    i = APP.index(start)
    return APP[i:APP.index(end_marker, i)]


HARNESS = r"""
const window = globalThis; globalThis.location = {origin: 'https://localhost'};
globalThis.navigator = {onLine: true};
let ME = {pubkey: 'a'.repeat(64)}; let _aiAuth=null, _aiAuthP=null, _aiAuthPAt=0, _aiToken='';
function _setAiToken(t){ _aiToken=t; }
function applyTermGate(){} function applyMediaGate(){}
async function sign(){ return {id:'x'}; }
globalThis.btoa = s => Buffer.from(s).toString('base64');
%s
%s
(async () => {
  const out = {};
  for (const base of ['http://o2c7ssznoqr3xjfjtewxi2gerrbglckdm5y54lvsev4kv3ahjh2bf4qd.onion', 'https://poster.place']) {
    window.__PC_API_BASE__ = base; _aiAuthP = null;
    globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
    try { await ensureAiSession(); out[base] = 'resolved?'; } catch (e) { out[base] = {msg: e.message, offline: !!e.offline}; }
  }
  console.log(JSON.stringify(out));
})();
"""


def test_the_error_names_the_server_and_explains_an_onion(tmp_path):
    fn = _slice("  function _unreachableWhy(", "  async function ensureAiSession(")
    ens = APP[APP.index("  async function ensureAiSession("):]
    # the function ends where the next top-level `function` of the module begins
    depth, i = 0, ens.index("{")
    for j in range(i, len(ens)):
        depth += {"{": 1, "}": -1}.get(ens[j], 0)
        if depth == 0:
            ens = ens[:j + 1]
            break
    f = tmp_path / "h.js"
    f.write_text(HARNESS % (fn, ens))
    got = json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True, check=True).stdout)
    onion = got["http://o2c7ssznoqr3xjfjtewxi2gerrbglckdm5y54lvsev4kv3ahjh2bf4qd.onion"]
    assert "Orbot" in onion["msg"] and "VPN mode" in onion["msg"] and ".onion" in onion["msg"], onion
    assert onion["offline"] is True
    clear = got["https://poster.place"]
    assert clear["msg"] == "could not establish your app session: could not reach poster.place", clear
