from scripts.prune_desktop_releases import stale_tags


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
