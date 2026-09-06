"""Execute the root helper's verifier on synthetic identities and a temporary challenge store."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from app.services.nostr import bech32
from app.services.nostr.event import build_event

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'os/bin/pc-session-auth'


@pytest.fixture
def auth(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader('pc_os_auth_test', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE', tmp_path / 'challenges')
    monkeypatch.setattr(module, 'CRYPTO', ROOT / 'app/services/nostr')
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('SUDO_UID', '4242')
    return module


def identity(seed=1):
    key = bytes([seed]) * 32
    event = build_event(key, 27235, '')
    return key, bech32.encode('npub', bytes.fromhex(event['pubkey']))


def invoke(auth, monkeypatch, capsys, mode, npub, action, envelope=None):
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), mode, npub, action])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode())))
    auth.main()
    return json.loads(capsys.readouterr().out)


def envelope(key, challenge, action='switch', payload='{"sess":{}}'):
    event = build_event(key, 27235, hashlib.sha256(payload.encode()).hexdigest(), tags=[
        ['t', 'posterchanos'], ['action', action], ['challenge', challenge]])
    return {'event': event, 'payload': payload}


def test_valid_owner_can_switch_once(auth, monkeypatch, capsys):
    key, npub = identity()
    challenge = invoke(auth, monkeypatch, capsys, 'challenge', npub, 'switch')['challenge']
    body = envelope(key, challenge)
    assert invoke(auth, monkeypatch, capsys, 'verify', npub, 'switch', body) == {'sess': {}}
    with pytest.raises(FileNotFoundError):
        invoke(auth, monkeypatch, capsys, 'verify', npub, 'switch', body)


@pytest.mark.parametrize('mutation', ['wrong-key', 'wrong-action', 'wrong-payload', 'public-note', 'expired'])
def test_untrusted_identity_handoffs_are_rejected(auth, monkeypatch, capsys, mutation):
    key, npub = identity()
    challenge = invoke(auth, monkeypatch, capsys, 'challenge', npub, 'switch')['challenge']
    body = envelope(key, challenge)
    if mutation == 'wrong-key': body = envelope(identity(2)[0], challenge)
    if mutation == 'wrong-action': body = envelope(key, challenge, 'provision')
    if mutation == 'wrong-payload': body['payload'] = '{"sess":{"attacker":true}}'
    if mutation == 'public-note': body['event'] = build_event(key, 1, 'public post')
    if mutation == 'expired':
        path = auth.STATE / '4242.json'
        saved = json.loads(path.read_text()); saved['expires'] = 1; path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        invoke(auth, monkeypatch, capsys, 'verify', npub, 'switch', body)


def test_installer_and_overlay_ship_identical_auth_helpers():
    for name in ['pc-session-auth', 'pc-session-switch', 'pc-provision-user']:
        assert (ROOT / 'os/bin' / name).read_bytes() == (
            ROOT / 'os/overlay/app-misc/posterchanos-shell/files' / name).read_bytes()
