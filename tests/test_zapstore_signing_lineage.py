from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_zapstore_publisher_advertises_rotated_signing_lineage():
    workflow = (ROOT / ".github/workflows/android.yml").read_text()
    patch = (ROOT / "scripts/zsp-signing-lineage.patch").read_text()

    assert "ZSP_ANDROID_PREVIOUS_CERTIFICATE_HASHES" in workflow
    assert "scripts/zsp-signing-lineage.patch" in workflow
    assert "eddf3a7983df49221a5ace0d0ca52c899d34eb88a4155b0829b05c0afc31f342" in workflow
    assert 'nostr.Tag{\"apk_certificate_hash\", fingerprint}' in patch
    assert "fingerprint != strings.ToLower(meta.CertFingerprint)" in patch
    assert "relay reported duplicate but event %s cannot be retrieved" in patch
    assert "nostr.Filter{IDs: []string{event.ID}, Limit: 1}" in patch
