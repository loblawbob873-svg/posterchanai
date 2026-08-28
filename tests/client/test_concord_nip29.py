from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_nip29_runtime():
    result = subprocess.run(
        ["node", str(ROOT / "tests/client/concord_nip29_runtime.mjs")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_signer_only_self_decrypt_and_read_only_write_boundary():
    app = (ROOT / "static/js/client/app.js").read_text()
    concord = (ROOT / "static/js/client/concord.js").read_text()
    relay = (ROOT / "static/js/client/relay.js").read_text()
    assert "nip04dec: (peer, ct)" in app
    assert "verifyRelayEvents:" in app
    assert "NIP-29 sending is read-only" in concord
    assert "else if(loaded&&loaded.protocol==='nip29')await hydrateNip29Room" in concord
    assert "exact=false" in relay and "exact || !this._conns.has(u)" in relay
    assert "...(p.relayUrls?p.relayUrls():[]),...CORD_RELAYS" in concord
    assert "for(const relay of membership.relays)" not in concord
