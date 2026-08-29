from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'static/js/client/budget.js').read_text()


def test_empty_budget_ciphertext_is_rejected_before_the_provider():
    load = SRC[SRC.index('async function _load(){'):SRC.index('function save(){')]
    validation = load.index("if(!sealed) throw new Error")
    decrypt = load.index('PC.nip44dec(ME().pubkey, sealed)')
    assert validation < decrypt
    assert 'signer was not asked' in load
    assert "ev.content||''" not in load


def test_oversized_budget_is_rejected_before_nip44_encrypt():
    save = SRC[SRC.index('function save(){'):SRC.index('// Start the month over')]
    boundary = save.index('bytes > NIP44_MAX')
    encrypt = save.index('PC.nip44enc(ME().pubkey, plain)')
    assert boundary < encrypt
    assert '65535' in SRC[SRC.index('const NIP44_MAX'):SRC.index('const ME =')]
    assert 'Chunking would invent' in save


def test_real_budget_module_never_sends_invalid_provider_requests():
    run = subprocess.run(
        ['node', str(Path(__file__).with_name('budget_nip44_boundary_sim.js'))],
        text=True, capture_output=True, check=True,
    )
    got = json.loads(run.stdout)
    assert 'empty or damaged' in got['emptyError']
    assert '65535 bytes' in got['largeError']
    assert got['decrypts'] == 1       # valid record only; empty record never reaches the provider
    assert got['encrypts'] == 0       # oversized plaintext never reaches the provider
    assert got['publishes'] == 0
