from pathlib import Path

import pytest

from scripts.prune_desktop_releases import overlay_release, stale_tags


def test_prunes_only_old_versioned_desktop_releases():
    releases = [
        {"tagName": "desktop-latest"},
        {"tagName": "apk-latest"},
        {"tagName": "extension-latest"},
        {"tagName": "desktop-v1.0.9"},
        {"tagName": "desktop-v1.0.11"},
        {"tagName": "desktop-v1.0.10"},
        {"tagName": "beta-3"},
    ]
    assert stale_tags(releases, keep=2, protect=set()) == ["desktop-v1.0.9"]


def test_explicitly_protected_overlay_release_is_never_pruned():
    releases = [
        {"tagName": "desktop-v1.0.12"},
        {"tagName": "desktop-v1.0.11"},
        {"tagName": "desktop-v1.0.10"},
    ]
    assert stale_tags(releases, keep=1, protect={"desktop-v1.0.10"}) == ["desktop-v1.0.11"]


def test_default_retention_is_one_release(monkeypatch):
    source = (__import__("pathlib").Path(__file__).parents[1] / "scripts/prune_desktop_releases.py").read_text()
    assert 'parser.add_argument("--keep", type=int, default=1)' in source


def test_overlay_pin_is_automatically_protected(tmp_path):
    (tmp_path / "posterchan-desktop-1.0.1174.ebuild").write_text("EAPI=8\n")
    assert overlay_release(tmp_path) == "desktop-v1.0.1174"


def test_ambiguous_overlay_refuses_pruning(tmp_path):
    (tmp_path / "posterchan-desktop-1.0.1.ebuild").write_text("EAPI=8\n")
    (tmp_path / "posterchan-desktop-1.0.2.ebuild").write_text("EAPI=8\n")
    with pytest.raises(RuntimeError, match="expected one"):
        overlay_release(tmp_path)


def test_main_always_adds_the_overlay_release_to_protection():
    source = (__import__("pathlib").Path(__file__).parents[1] / "scripts/prune_desktop_releases.py").read_text()
    assert "protect.add(overlay_release())" in source
