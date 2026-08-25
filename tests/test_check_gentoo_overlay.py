from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK = (ROOT / "scripts/check_gentoo_overlay.py").read_text()


def test_live_overlay_check_compares_public_ebuild_manifest_release_and_checksum():
    assert "posterchan-overlay.git" in CHECK
    assert 'for name in (local_ebuild, "Manifest")' in CHECK
    assert 'base + ".sha512"' in CHECK
    assert 'Request(base, method="HEAD")' in CHECK
    assert "published != digest" in CHECK
