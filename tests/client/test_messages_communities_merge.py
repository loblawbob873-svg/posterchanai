from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
HTML = (ROOT / "templates/client.html").read_text()
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()


def test_only_messages_is_a_top_level_chat_launcher():
    assert 'data-view="messages"' in HTML
    assert 'data-view="concord"' not in HTML
    assert "{ view:'concord', into:'#disc-sub'" not in APP
    assert "['concord','concord','Concord']" not in APP


def test_messages_separates_direct_messages_and_communities_inside_one_app():
    assert 'class="messages-tabs"' in APP
    assert 'id="messages-communities"' in APP
    assert "switchView('concord')" in APP
    assert 'class="messages-tabs"' in CONCORD
    assert 'id="messages-direct"' in CONCORD
    assert "p.switchView('messages')" in CONCORD


def test_android_launcher_uses_unambiguous_texts_and_messages_names():
    assert 'new Tile(VIEW_TEXTS,      "Texts"' in TILES
    assert 'new Tile("messages",      "Messages"' in TILES
    assert 'new Tile("concord"' not in TILES


def test_old_concord_routes_remain_for_invites_and_saved_shortcuts():
    assert "renderModuleView('concord','concord.js','PCConcord','render')" in APP
    assert "if(v==='concord') $('#view-title').textContent='Messages'" in APP
