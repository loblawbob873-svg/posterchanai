"""A Concord message must not be lost because the tunnel refused the socket.

Reported 2026-10-13 as "concord not even sending messages on my android now. broken". Measured on poster.place:
in one hour 137 tunnels opened and 2,314 were refused -- every refused dial carried a VALID ticket to an allowed
relay, so it was the per-member cap: Concord keeps a live subscription per relay per room, more than the tunnel
allowed one member, and every SEND after that was a socket closed before it opened. A send is one signed event
(the same id however many times it goes out), so a tunnel that cannot carry it is retried DIRECT once: the message
arrives, and the next dial tries the tunnel again.

Runs the shipped TUNNEL/cordVia/cordDeps/cordRelay block of concord.js under node against stub doors.
Run: venv-unified/bin/python -m pytest tests/client/test_concord_send_survives_a_refused_tunnel.py -q
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
BLOCK = SRC[SRC.index("  const TUNNEL={"):SRC.index("  Object.defineProperty(window,'PCConcordRelay'")]

HARNESS = r"""
const window = {__PC_API_BASE__: 'https://home.example'};
globalThis.fetch = async () => ({ok: true, status: 200, json: async () => ({ticket: 'T', ttl: 3600})});
function normalizeRelay(u){ return String(u||'').replace(/\/+$/,''); }
function homeRelayUrl(p){ return 'wss://home.example'; }
const dials = [];
const tunnelUp = TUNNEL_UP;
function sock(u, via){ const url = via ? via(u) : u; dials.push(url); return !url.includes('/ws/concord-relay') || tunnelUp; }
const raw = {
  homeRelay: () => 'wss://home.example',
  relayPublishTo: async (relays, ev, o={}) => relays.filter(u => sock(u, o.via)).length,
  relayPublishRoom: async (relays, ev, auth=null, via=null) => { const n = relays.filter(u => sock(u, via)).length;
    return {ok: n > 0, accepted: n, msg: n ? '' : 'room relay connection closed'}; },
  publishNip29Authed: async (relay, t, via=null) => { if (!sock(relay, via)) throw new Error('closed'); return {id: 'x'}; },
  relayQueryFrom: async () => [],
};
const R = {
  publishTo: async (urls, ev, o={}) => { const n = urls.filter(u => sock(u, o.via)).length;
    return o.detailed ? {ok: n > 0, accepted: n, msg: n ? '' : 'room relay connection closed'} : n; },
  publishAuthed: async (u, ev, sign, o={}) => sock(u, o.via) ? {ok: true} : {ok: false, msg: 'closed'},
  queryFrom: async () => [], subscribeFrom: () => ({close(){}}),
};
window.__PC = raw; const PC = () => cordDeps(window.__PC);
BLOCK
(async () => {
  await refreshTunnel();
  const ev = {id: 'e1', kind: 1059};
  const out = {
    room: await PC().relayPublishRoom(['wss://far.example'], ev),
    to: await PC().relayPublishTo(['wss://far.example'], ev, {}),
    rTo: await cordRelay(R).publishTo(['wss://far.example'], ev, {detailed: true}),
    authed: await cordRelay(R).publishAuthed('wss://far.example', ev, null, {}),
    nip29: await PC().publishNip29Authed('wss://far.example', {kind: 9}).then(() => true, () => false),
    dials,
  };
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def _run(tunnel_up):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = HARNESS.replace("BLOCK", BLOCK).replace("TUNNEL_UP", "true" if tunnel_up else "false")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_send_the_tunnel_refuses_still_reaches_the_relay():
    out = _run(tunnel_up=False)
    assert out["room"]["ok"], out
    assert out["to"] == 1, out
    assert out["rTo"]["ok"], out
    assert out["authed"]["ok"], out
    assert out["nip29"] is True, out
    # it went to the tunnel FIRST, and only then direct
    assert any("/ws/concord-relay" in d for d in out["dials"]) and "wss://far.example" in out["dials"], out


def test_a_working_tunnel_carries_every_send_and_nothing_goes_direct():
    out = _run(tunnel_up=True)
    assert out["room"]["ok"] and out["to"] == 1 and out["rTo"]["ok"] and out["authed"]["ok"] and out["nip29"], out
    assert all("/ws/concord-relay" in d for d in out["dials"]), out
