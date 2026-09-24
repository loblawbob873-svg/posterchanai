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


def _published(tmp_path, version):
    import subprocess
    repo = tmp_path / "overlay"
    d = repo / "app-misc" / "posterchan-desktop"
    d.mkdir(parents=True)
    (d / f"posterchan-desktop-{version}.ebuild").write_text("EAPI=8\n")
    (d / "Manifest").write_text("")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "o"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    return str(repo)


def test_the_published_overlays_pin_is_read_from_the_published_repo(tmp_path):
    """What installed machines download is the PUBLISHED overlay's pin, which lags the committed one
    until a deploy publishes it -- 1.0.1668 was pruned while still published, and every
    `update-posterchan` answered 404."""
    from scripts.prune_desktop_releases import published_release
    assert published_release(_published(tmp_path, "1.0.1668")) == "desktop-v1.0.1668"


def test_an_unreadable_published_overlay_prunes_nothing(monkeypatch, capsys):
    import json
    import sys
    from scripts import prune_desktop_releases as p
    calls = []
    monkeypatch.setattr(p.subprocess, "check_output", lambda *a, **k: json.dumps(
        [{"tagName": "desktop-v1.0.1"}, {"tagName": "desktop-v1.0.2"}, {"tagName": "desktop-v1.0.3"}]))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(p, "overlay_release", lambda: "desktop-v1.0.3")

    def unreadable(url=p.PUBLISHED):
        raise OSError("gentoo.poster.place unreachable")
    monkeypatch.setattr(p, "published_release", unreadable)
    monkeypatch.setattr(sys, "argv", ["prune", "--repo", "x/y"])
    assert p.main() == 0 and calls == [], "releases were deleted without knowing what installed machines need"
    monkeypatch.setattr(p, "published_release", lambda url=p.PUBLISHED: "desktop-v1.0.1")
    p.main()
    assert [c[3] for c in calls if c[:3] == ["gh", "release", "delete"]] == ["desktop-v1.0.2"]
