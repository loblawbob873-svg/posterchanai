from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text()


def test_persisted_hydrated_flag_does_not_skip_renderer_cache_restore():
    """A new process has empty renderer maps even when saved metadata says hydrated."""
    server = JS.split("$$('[data-cc-server]')", 1)[1].split("$$('[data-cc-discover]')", 1)[0]
    channel = JS.split("$$('[data-cc-channel]')", 1)[1].split("$$('[data-cc-star]')", 1)[0]
    assert "const hydratedRoomViews=new Set()" in JS
    assert "!hydratedRoomViews.has(roomIdentity(loaded))" in server
    assert "!hydratedRoomViews.has(roomIdentity(room))" in channel
    assert "!loaded.cord.hydrated" not in server
    assert "!room.cord.hydrated" not in channel


def test_late_network_hydration_preserves_reader_scroll():
    hydrate = JS.split("async function hydrateRoomStreams(p,index", 1)[1].split(
        "async function publishCordNative", 1
    )[0]
    assert "hydratedRoomViews.add(identity)" in hydrate
    tail = hydrate.split("stillSelected=", 1)[1]
    assert "if(stillSelected)backgroundRender();" in tail
    assert "scrollChatBottom()" not in tail


def test_message_store_keys_cannot_collide_between_community_id_only_rooms():
    helper = JS.split("function channelStoreId", 1)[1].split("function channelsOf", 1)[0]
    assert "room.naddr||room.communityId||room.url" in helper
