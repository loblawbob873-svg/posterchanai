"""Opening PosterChan screens must not turn navigation into a music transport command."""

import re
from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")
ROOT = Path(__file__).parents[2]


def test_reopening_music_keeps_the_existing_track_and_position():
    start = APP.index("function openMusic()")
    body = APP[start:APP.index("\n  async function renderBlossom", start)]
    assert "renderMusicApp();" in body
    assert "MusicPlayer.play(" not in body
    assert "MusicPlayer.shuffle" not in body


def test_launcher_music_route_uses_the_non_restarting_entrypoint():
    phone = (Path(__file__).parents[2] / "static/js/client/phoneshell.js").read_text(encoding="utf-8")
    assert "PC.openMusic()" in phone


def test_widget_success_opens_regular_music_interface_without_restarting_track():
    start = APP.index("consumeLaunch(){")
    body = APP[start:APP.index("\n    _media(){", start)]
    assert "if(done){ this._render(); renderMusicApp(); return; }" in body
    assert "MusicPlayer.play(" not in body
    assert "_audioEl =" not in body


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


def test_closing_desktop_music_window_does_not_kill_background_playback():
    """Closing a frame is not "stop the music" — EXCEPT when the frame was the last thing that
    could have stopped it, which is the one case the desktop has no answer for (there is no
    floating player under `html.os-on`). See tests/client/test_music_desktop_controls.py.

    This test went VACUOUS when that exception was added: it only asked whether the OLD blanket
    `stopMusic` call was absent, so it passed while playback was being killed by a different call
    on a path it never looked at. A test that cannot fail for the thing it is named after is worse
    than no test, because it reads as coverage. It asserts the narrow rule now, so a future blanket
    pause — the regression it was written to prevent — still turns it red.
    """
    os_src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    start = os_src.index("function closeWin(w, opts)")
    body = os_src[start:os_src.index("\n  function minimise", start)]
    assert "PC().stopMusic" not in body
    assert "PC().syncPlayer" in body
    # Playback may only be ended by the three conditions together. Any unconditional pause here —
    # or one that drops a condition — is the background-player regression coming back.
    # Comments in here discuss stopMusic at length; CODE is what runs. Strip the prose first, or
    # this counts the explanation of the old bug as a second way to cause it.
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    stops = [line for line in code.splitlines()
             if ".close()" in line or ".pause()" in line or "stopMusic" in line]
    assert len(stops) == 1, "exactly one place in closeWin may end playback: " + repr(stops)
    guard = body[:body.index(stops[0])].rsplit("if(", 1)[-1]
    for condition in ("opts.user", "doc:music", "__music", "!musicSurface()"):
        assert condition in guard, (
            "closing a window ends playback without checking " + condition + " — a handoff, a "
            "native reconcile or a folder cleanup would then stop the music")
