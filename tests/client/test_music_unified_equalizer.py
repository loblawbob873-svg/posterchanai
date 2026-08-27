from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_music_app_has_one_transport_and_equalizer():
    body = APP[APP.index("function renderMusicApp(){"):APP.index("function _musicPhoneSettings", APP.index("function renderMusicApp(){"))]
    assert 'id="ma-shuffle"' in body
    assert 'id="ma-play"' in body
    assert 'id="ma-eq"' in body
    assert all(f'data-band="{band}"' in body for band in ("low", "mid", "high"))
    assert all(f'data-eq="{preset}"' in body for preset in ("flat", "bass", "vocal", "bright"))
    assert "MusicPlayer.bindEqualizer(feed)" in body
    assert 'id="ma-viz"' in body
    assert "MusicPlayer._startViz()" in body


def test_equalizer_is_persistent_and_part_of_the_single_audio_graph():
    assert "localStorage.getItem('pc_music_eq')" in APP
    assert "localStorage.setItem('pc_music_eq'" in APP
    assert "createBiquadFilter()" in APP
    assert "v.src.connect(low); low.connect(mid); mid.connect(high); high.connect(v.an)" in APP
    assert "if(v.an) return true" in APP, "the player must not create a second MediaElementSource"
    assert "const appCv=document.getElementById('ma-viz')" in APP
    assert "const cv=appCv || floating" in APP


def test_visualizer_is_compact_so_the_library_keeps_the_screen():
    assert ".ma-viz{display:block;position:absolute" in CSS
    assert "inset:0;width:100%;height:100%;opacity:.34" in CSS
    assert ".music-app{display:flex;flex-direction:column;gap:9px" in CSS
    assert ".music-app .music-list{flex:1;min-height:0;overflow-y:auto}" in CSS
    mobile = CSS[CSS.index("@media(max-width:560px)"):]
    assert ".ma-art{display:none}" in mobile
    assert ".ma-ctl{flex:0 0 auto" in mobile


def test_phone_transport_and_equalizer_fit_without_horizontal_overflow():
    assert ".ma-ctl{display:flex;align-items:center;justify-content:center" in CSS
    assert ".ma-eq-bands{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in CSS
    mobile = CSS[CSS.index("@media(max-width:560px)"):]
    assert ".ma-eq-bands{grid-template-columns:1fr" in mobile


def test_desktop_does_not_show_the_competing_floating_player():
    assert "html.os-on #music-player{display:none}" in CSS


def test_android_uses_full_music_app_and_native_background_controls_only():
    start = APP.index("_render(){", APP.index("const MusicPlayer"))
    render = APP[start:APP.index("_wire(){", start)]
    assert "_capPlugin('MusicControls','addListener')" in render
    assert "d.classList.add('hidden')" in render


def test_music_car_setting_does_not_expose_debug_details():
    assert 'id="ma-cardiag"' not in APP
    assert 'id="ma-carnote"' not in APP
    assert ".ma-car-note" not in CSS


def test_bluetooth_autoplay_is_phone_settings_not_music_chrome():
    body = APP[APP.index("function renderMusicApp(){"):APP.index("function _musicPhoneSettings")]
    phone = (ROOT / "static/js/client/phoneshell.js").read_text()
    assert 'id="ma-autobt"' not in body
    assert 'id="ma-car"' not in body
    assert "musicPhoneSettings: _musicPhoneSettings" in APP
    assert 'id="ps-autobt"' in phone
    assert "setAutoplayBluetooth(box.checked)" in phone


def test_phone_library_actions_are_one_compact_row_with_count_below():
    body = APP[APP.index("function _renderMusicList"):
               APP.index("grid.onclick = async", APP.index("function _renderMusicList"))]
    primary = body[body.index('class="music-head-primary"'):
                   body.index('class="music-count ')]
    assert all(f'id="{control}"' in primary
               for control in ("mus-shuffle", "mus-refresh", "mus-delall"))
    assert primary.index('id="mus-shuffle"') < primary.index('id="mus-refresh"') < primary.index('id="mus-delall"')
    assert "'Delete All'" in primary
    assert 'class="music-count muted small"' in body

    mobile = CSS[CSS.index("@media(max-width:560px){", CSS.index(".music-head{")):]
    assert ".music-head-primary{order:1;display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in mobile
    assert ".music-count{order:2;flex:1 1 100%;width:100%" in mobile


def test_library_count_describes_every_visible_row_not_only_playable_tracks():
    body = APP[APP.index("function _renderMusicList"):
               APP.index("grid.onclick = async", APP.index("function _renderMusicList"))]
    count = body[body.index('class="music-count muted small"'):
                 body.index('class="music-head-secondary"')]
    assert "tracks.length + ' track'" in count
    assert "tracks.length===1?'':'s'" in count
    assert "${gone} missing from the server" in count
    assert "live + ' track'" not in count
    assert 'aria-live="polite"' in count
