from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_music_app_has_one_transport_and_equalizer():
    body = APP[APP.index("function renderMusicApp(){"):APP.index("function _bindMusicCar", APP.index("function renderMusicApp(){"))]
    assert 'id="ma-shuffle"' in body
    assert 'id="ma-play"' in body
    assert 'id="ma-eq"' in body
    assert all(f'data-band="{band}"' in body for band in ("low", "mid", "high"))
    assert all(f'data-eq="{preset}"' in body for preset in ("flat", "bass", "vocal", "bright"))
    assert "MusicPlayer.bindEqualizer(feed)" in body


def test_equalizer_is_persistent_and_part_of_the_single_audio_graph():
    assert "localStorage.getItem('pc_music_eq')" in APP
    assert "localStorage.setItem('pc_music_eq'" in APP
    assert "createBiquadFilter()" in APP
    assert "v.src.connect(low); low.connect(mid); mid.connect(high); high.connect(v.an)" in APP
    assert "if(v.an) return true" in APP, "the player must not create a second MediaElementSource"


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
