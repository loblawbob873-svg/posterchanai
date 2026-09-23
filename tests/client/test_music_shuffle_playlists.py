"""Shuffle inside a playlist stays inside the playlist, and tapping a song does not turn it off.

Reported as "shuffle is no longer working on playlists". Two causes:

  * every path that rebuilt the play queue -- ⏭ on an empty queue, a track the queue did not hold,
    the desktop widget's Shuffle -- rebuilt it from the whole LIBRARY. Measured with the shipped
    player: 140 of 200 shuffle picks in a 3-song playlist were songs from outside it;
  * tapping a song inside a playlist set `shuffle = false`, silently, while the shuffle button
    still showed it on (own playlists and shared ones alike).

`music_shuffle_runtime.cjs` RUNS the shipped musicplayer.js. The tap rule is checked on the shipped
handlers, where it lives.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def results():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", os.path.join(HERE, "music_shuffle_runtime.cjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.stdout.strip(), r.stderr[-2000:]
    return json.loads(r.stdout)


@pytest.mark.parametrize("scenario", [
    "shuffle in a playlist never leaves it",
    "the desktop widget's Shuffle stays in the chosen playlist",
    "a song ending moves on inside the playlist, shuffled",
    "with no playlist chosen, shuffle covers the library",
    "shuffle picking the song already playing restarts it, never pauses",
])
def test_scenario(results, scenario):
    assert scenario in results, sorted(results)
    assert results[scenario]["ok"], results[scenario]["detail"]


def _no_comments(s):
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


def test_tapping_a_song_in_a_playlist_leaves_shuffle_alone():
    music = _no_comments(_read("static", "js", "client", "music.js"))
    tap = music[music.index("if(b.classList.contains('track-play')){"):]
    tap = tap[:tap.index("MusicPlayer.play(sha)")]
    assert not re.search(r"\.shuffle\s*=[^=]", tap), "tapping a song in a playlist changes the shuffle setting again"
    share = _no_comments(_read("static", "js", "client", "musicshare.js"))
    row = share[share.index("if(b.id === 'msh-shuffle'){"):]
    row = row[row.index("else if(M)"):]
    row = row[:row.index("\n")]
    assert not re.search(r"\.shuffle\s*=[^=]", row), "tapping a song in a shared playlist changes the shuffle setting"


def test_leaving_a_playlist_rebuilds_the_queue_from_the_new_selection():
    """refreshQueue reads MusicPlayer._pl, so it must run AFTER the selection moves."""
    music = _read("static", "js", "client", "music.js")
    bar = music[music.index("function _bindPlBar(root, repaint){"):]
    bar = bar[:bar.index("repaint(); });")]
    assert bar.index("MusicPlayer._pl =") < bar.index("MusicPlayer.refreshQueue()"), bar
