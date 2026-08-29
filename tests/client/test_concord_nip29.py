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
    membership = concord.split("async function nip29Memberships", 1)[1].split("async function nip29RelayQuery", 1)[0]
    # THE RECOVERY SOURCES ARE STILL REACHABLE, AND STILL ONLY ON AN EXPLICIT RECOVERY PASS. The
    # ordinary startup read is cache → the user's own pool → nothing else; `concord_nip29_runtime.mjs`
    # is what proves that behaviourally (it counts the sockets), so this pins only the two facts a
    # runtime harness cannot see: which relay sets exist, and that the long circuit is still bounded.
    assert "legacyRecovery?[...CORD_RELAYS,...LEGACY_RECOVERY_RELAYS]:CORD_RELAYS" in membership
    assert "!local.length&&p.relayQueryFrom" in membership, (
        "external membership relays are opened without first asking the cache and the user's pool"
    )
    assert "p.relayUrls" not in membership
    assert "allowBlocked:legacyRecovery,failureCooldown:1800000" in membership
    assert "for(const relay of membership.relays)" not in concord
