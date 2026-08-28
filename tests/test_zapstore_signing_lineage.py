from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_zapstore_publisher_advertises_rotated_signing_lineage():
    workflow = (ROOT / ".github/workflows/android.yml").read_text()
    patch = (ROOT / "scripts/zsp-signing-lineage.patch").read_text()

    assert "ZSP_ANDROID_PREVIOUS_CERTIFICATE_HASHES" in workflow
    assert "scripts/zsp-signing-lineage.patch" in workflow
    assert 'apply --unidiff-zero "$GITHUB_WORKSPACE/scripts/zsp-signing-lineage.patch"' in workflow
    assert "zsp-v0.4.11-posterchan-lineage-v3" in workflow
    assert "RELAY_URLS: wss://relay.poster.place,wss://relay.primal.net,wss://relay.zapstore.dev" in workflow
    assert "eddf3a7983df49221a5ace0d0ca52c899d34eb88a4155b0829b05c0afc31f342" in workflow
    assert 'nostr.Tag{\"apk_certificate_hash\", fingerprint}' in patch
    assert "fingerprint != strings.ToLower(meta.CertFingerprint)" in patch
    assert "relay reported duplicate but event %s cannot be retrieved" in patch
    assert "nostr.Filter{IDs: []string{event.ID}, Limit: 1}" in patch
    assert patch.index("accepted := make(map[string]bool") < patch.index("+\t\tif events.AppMetadata != nil")
    assert "if !accepted[url]" in patch
    assert "accepted[url] = false" in patch
    assert "p.publishToRelay(ctx, url, events.Release)" in patch
    assert "Zapstore AppCatalog rejected the APK signing-lineage asset" in workflow
    assert "software_asset -> wss://relay.zapstore.dev: FAILED" in workflow
    assert "continue-on-error: true" not in workflow
    assert '::error title=Zapstore publish failed' in workflow
    assert "exit 1" in workflow
