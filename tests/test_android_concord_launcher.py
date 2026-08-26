from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()


def test_concord_communities_are_inside_the_messages_launcher():
    assert 'new Tile("messages",      "Messages",      "speech"' in TILES
    assert 'new Tile("concord"' not in TILES
    assert 'android:value="concord"' not in MANIFEST
    assert 'android:name=".shortcut.Concord"' not in MANIFEST


def test_removed_nostr_chat_is_not_an_android_launcher_tile():
    assert 'new Tile("chat"' not in TILES
