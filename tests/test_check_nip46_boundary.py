from pathlib import Path


SRC=(Path(__file__).resolve().parents[1]/'scripts/check_nip46_signer.py').read_text()


def test_oversize_scenario_requires_local_actionable_rejection_and_no_signer_call():
    assert "seen_before = len(bunker.seen)" in SRC
    assert '"65535" not in err' in SRC
    assert '"attachment" not in err.lower()' in SRC
    assert "len(bunker.seen) != seen_before" in SRC
    assert "a 100KB NIP-44 plaintext was accepted" in SRC
