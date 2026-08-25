from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()


def test_concord_is_an_android_drawer_app_and_launcher_tile():
    block = MANIFEST.split('android:name=".shortcut.Concord"', 1)[1].split('</activity-alias>', 1)[0]
    assert 'android.intent.category.LAUNCHER' in block
    assert 'android:value="concord"' in block
    assert '@mipmap/ic_launcher_concord' in block
    assert 'new Tile("concord",       "Concord",       "concord"' in TILES


def test_removed_nostr_chat_is_not_an_android_launcher_tile():
    assert 'new Tile("chat"' not in TILES
