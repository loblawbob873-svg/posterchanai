"""Opening PosterChan screens must not turn navigation into a music transport command."""

from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")
ROOT = Path(__file__).parents[2]


def test_reopening_music_keeps_the_existing_track_and_position():
    start = APP.index("function openMusic()")
    body = APP[start:APP.index("\n  async function renderBlossom", start)]
    guard = body.index("if(MusicPlayer.cur || (_audioEl && _audioEl.src))")
    show = body.index("renderMusicApp();", guard)
    choose = body.index("MusicPlayer.play(")
    assert guard < show < choose
    assert "return;" in body[show:choose]


def test_launcher_music_route_uses_the_non_restarting_entrypoint():
    phone = (Path(__file__).parents[2] / "static/js/client/phoneshell.js").read_text(encoding="utf-8")
    assert "PC.openMusic()" in phone


def test_switching_posterchan_views_never_stops_the_player():
    start = APP.index("function switchView(v, quiet)")
    body = APP[start:APP.index("\n  function ", start + 20)]
    assert "MusicPlayer.close" not in body
    assert "_audioEl.pause" not in body
    assert "_audioEl =" not in body


def test_android_home_and_launcher_tiles_reuse_the_live_webview():
    main = (ROOT / "mobile/android/app/src/main/java/place/poster/app/MainActivity.java").read_text()
    home = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeActivity.java").read_text()
    pause = main[main.index("public void onPause()") : main.index("/**", main.index("public void onPause()"))]
    assert "Music" not in pause and ".pause(" not in pause
    landing = home[home.index("Intent i = new Intent(this, MainActivity.class)") :]
    landing = landing[: landing.index("startActivity(i)") + len("startActivity(i)")]
    assert "Intent.FLAG_ACTIVITY_SINGLE_TOP" in landing
    assert "Intent.FLAG_ACTIVITY_CLEAR_TOP" not in landing
