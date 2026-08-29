from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text()


def test_persisted_hydrated_flag_does_not_skip_renderer_cache_restore():
    """A new process has empty renderer maps even when saved metadata says hydrated."""
    activation = JS.split('async function activateJoinedRoom', 1)[1].split('function render()', 1)[0]
    channel = JS.split("$$('[data-cc-channel]')", 1)[1].split("$$('[data-cc-star]')", 1)[0]
    assert "const hydratedRoomViews=new Set()" in JS
    assert "!hydratedRoomViews.has(roomIdentity(room))" in activation
    assert "!hydratedRoomViews.has(roomIdentity(room))" in channel
    assert "!room.cord.hydrated" not in activation
    assert "!room.cord.hydrated" not in channel


def test_saved_active_room_uses_direct_automatic_hydration_not_a_dom_click():
    render = JS.split("function render()", 1)[1].split("function openNotification", 1)[0]
    activate = JS.split("async function activateJoinedRoom", 1)[1].split("function render()", 1)[0]
    assert "activateJoinedRoom(p,autoOpen,false,identity)" in render
    assert "querySelector(`[data-cc-server" not in render
    assert "hydrateRoomStreams(p,index,identity)" in activate
    assert "render();enterChatBottom();" in activate
    assert "expectedIdentity" in activate and "findIndex" in activate


def test_reentering_an_already_selected_room_kicks_history_immediately():
    render = JS.split("function render()", 1)[1].split("function openNotification", 1)[0]
    resume = JS.split('async function resumeActiveRoom', 1)[1].split('function render()', 1)[0]
    app = (ROOT / 'static/js/client/app.js').read_text()
    assert "const concordReentry=VIEW!=='concord'&&v==='concord'" in app
    assert 'PCConcord.wake&&PCConcord.wake()' in app
    assert 'if(resumeRequested)' in render
    assert 'resumeActiveRoom(p,identity)' in render
    assert 'function wake(){resumeRequested=true;}' in JS
    assert 'hydrateRoomStreams(p,index,identity)' in resume
    assert 'await refreshActiveChannel(p)' in resume
    assert 'scrollChatBottom' not in resume and 'enterChatBottom' not in resume


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
