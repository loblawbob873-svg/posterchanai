"""Opening PosterChan screens must not turn navigation into a music transport command."""

from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


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
