import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_shipped_relay_answers_auth_and_retries_private_read():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/nip78_auth_runtime.mjs")],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_app_gives_relay_transport_the_active_signer():
    src = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8", errors="ignore")
    assert "Relay.setAuthSigner" in src
    assert "signer.signEvent(Object.assign({}, tpl, {pubkey:ME.pubkey}))" in src


def test_extension_authenticates_before_replaying_private_vault_read():
    src = (ROOT / "extension/background.js").read_text(encoding="utf-8")
    assert "m[0] === 'AUTH'" in src
    assert "kind:22242" in src
    assert "['relay',url]" in src and "['challenge',String(m[1])]" in src
    assert "['REQ','pcvault'" in src
    auth_send = src.index("c.ws.send(JSON.stringify(['AUTH',a]))")
    auth_ok = src.index("if(c.authId && m[1] === c.authId)", auth_send)
    replay = src.index("c.ws.send(JSON.stringify(['REQ','pcvault'", auth_ok)
    assert auth_send < auth_ok < replay
    assert "if(c.authed)c.ws.send" in src


def test_android_sms_signer_answers_auth_with_same_archive_key():
    svc = (ROOT / "mobile/android/app/src/main/java/place/poster/app/signer/SignerRelayService.java").read_text()
    assert '"AUTH".equals(m.optString(0, ""))' in svc
    assert "SmsOutbox.signed(key, pubHex" in svc
    assert "22242" in svc
    assert 'Arrays.asList("relay", url)' in svc
    assert 'Arrays.asList("challenge", m.optString(1, ""))' in svc
