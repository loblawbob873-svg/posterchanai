from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()


def test_communities_is_its_own_launcher_tile():
    """It used to be INSIDE the Messages tile, because Communities was a tab inside Messages. With
    that tab gone, a phone-shell user opening Messages finds no way to a community at all — the
    launcher catalogue is a THIRD surface, separate from the sidebar and the phone's ☰ More sheet,
    and a view missing from it is a view with no icon on the home screen."""
    assert 'new Tile("messages",      "Messages",      "speech"' in TILES
    assert 'new Tile("concord"' in TILES, (
        "Communities has no launcher tile, so on the phone shell there is no icon for it and the "
        "Messages tile no longer contains it either")
    # It is a VIEW tile, not a second activity: no manifest alias, no extra drawer icon.
    assert 'android:name=".shortcut.Concord"' not in MANIFEST


def test_removed_nostr_chat_is_not_an_android_launcher_tile():
    assert 'new Tile("chat"' not in TILES
