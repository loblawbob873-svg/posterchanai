from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_nip29_runtime():
    result = subprocess.run(
        ["node", str(ROOT / "tests/client/concord_nip29_runtime.mjs")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_signer_decrypt_and_authenticated_write_boundary():
    app = (ROOT / "static/js/client/app.js").read_text()
    concord = (ROOT / "static/js/client/concord.js").read_text()
    relay = (ROOT / "static/js/client/relay.js").read_text()
    assert "nip04dec: (peer, ct)" in app
    assert "verifyRelayEvents:" in app
    assert "publishNip29Authed:" in app
    assert "kind:22242" in app and "['challenge',String(challenge||'')]" in app
    assert "pubkey:ME.pubkey" in app
    assert "room&&room.protocol==='nip29'?publishNip29Message" in concord
    assert "room.protocol!=='nip29'" in concord
    assert "else if(room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,index)" in concord
    assert "exact=false" in relay and "exact || !this._conns.has(u)" in relay
    assert "...(p.relayUrls?p.relayUrls():[]),...CORD_RELAYS" in concord
    assert "for(const relay of membership.relays)" not in concord
