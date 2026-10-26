"""The Music app's Songs / Albums / Artists views group the library the way a music client does.

Runs the SHIPPED window.PCMusicGroups (top of static/js/client/music.js) under node against library entries shaped
exactly as musiclib.musicEntries returns them -- {sha, m:{name, ts, tg:{...}}} -- where `tg` is the tag index the
scan writes into the encrypted files index. Every case here is one a real library has: an album spread over many
uploads, track numbers written "3/12", a second disc, a compilation, the same album typed two ways, untagged files.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")

RUN = r"""
const fs=require('fs');const src=fs.readFileSync(process.argv[1],'utf8');
const ctx={String,Number,Math,Object,Array,Map,Set,JSON};ctx.window=ctx;ctx.globalThis=ctx;
require('vm').runInNewContext(src,ctx);
const G=ctx.PCMusicGroups, lib=JSON.parse(fs.readFileSync(0,'utf8'));
const sha=e=>e.sha;
const albums=G.albums(lib).map(a=>({album:a.album,artist:a.artist,year:a.year,unknown:a.unknown,tracks:a.tracks.map(sha)}));
const artists=G.artists(lib).map(a=>({name:a.name,unknown:a.unknown,albums:a.albums.map(x=>x.album),tracks:a.tracks.map(sha)}));
const sorts={};for(const by of ['title','artist','album','added']) sorts[by]=G.sortSongs(lib,by).map(sha);
const search={song:lib.filter(e=>G.matchSong(e,'NIGHT')).map(sha),
  album:G.albums(lib).filter(a=>G.matchAlbum(a,'abbey')).map(a=>a.album),
  artist:G.artists(lib).filter(a=>G.matchArtist(a,'beat')).map(a=>a.name)};
console.log(JSON.stringify({albums,artists,sorts,search}));
"""


def e(sha, name, ts=0, **tg):
    m = {"name": name, "ts": ts, "enc": True}
    if tg:
        m["tg"] = {"v": 1, **tg}
    return {"sha": sha, "m": m}


LIBRARY = [
    # FIRST on purpose: its spellings are the minority, so naming an album/artist by the first file seen is caught.
    e("ab5", "octopus", 3, t="Octopus's Garden", a="the beatles", al="ABBEY ROAD", n=5),
    # Abbey Road, uploaded out of order, typed two ways ("Abbey Road" / "abbey  road "), two discs.
    e("ab3", "come_together", 5, t="Come Together", a="The Beatles", al="Abbey Road", n=1, y="1969"),
    e("ab1", "something", 9, t="Something", a="The Beatles", al="abbey  road ", n=2),
    e("ab9", "her_majesty", 1, t="Her Majesty", a="The Beatles", al="Abbey Road", n=1, d=2),
    # A compilation: two artists, one album artist -> ONE album.
    e("c1", "a", 2, t="Track A", a="Artist One", aa="Various Artists", al="Night Drive", n=2),
    e("c2", "b", 4, t="Track B", a="Artist Two", aa="Various Artists", al="Night Drive", n=1),
    # The same album NAME by two artists with no album artist -> TWO albums.
    e("g1", "x", 6, t="Greatest X", a="Band X", al="Greatest Hits"),
    e("g2", "y", 7, t="Greatest Y", a="Band Y", al="Greatest Hits"),
    # Untagged / partially tagged: kept, never dropped.
    e("u1", "mystery_file", 8),
    e("u2", "only_a_title", 10, t="Only A Title"),
    e("u3", "artist_no_album", 11, t="Loose Single", a="Solo Act"),
]


@pytest.fixture(scope="module")
def got():
    done = subprocess.run([NODE, "-e", RUN, str(ROOT / "static/js/client/music.js")], input=json.dumps(LIBRARY),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _album(got, name, artist=None):
    hits = [a for a in got["albums"] if a["album"].lower().replace("  ", " ").strip() == name.lower()
            and (artist is None or a["artist"] == artist)]
    assert len(hits) == 1, (name, artist, got["albums"])
    return hits[0]


def test_one_album_however_its_name_was_typed_and_in_disc_then_track_order(got):
    abbey = [a for a in got["albums"] if a["album"].lower().strip().replace("  ", " ") == "abbey road"]
    assert len(abbey) == 1, ("the same album typed two ways became two albums", abbey)
    # Disc 1 tracks 1, 2, 5 -- then disc 2 track 1. Not upload order, not alphabetical.
    assert abbey[0]["tracks"] == ["ab3", "ab1", "ab5", "ab9"], abbey[0]
    assert abbey[0]["year"] == "1969"
    # Named the way MOST of its tracks spell it -- not by whichever file sorted first.
    assert abbey[0]["album"] == "Abbey Road" and abbey[0]["artist"] == "The Beatles", abbey[0]
    beatles = [a["name"] for a in got["artists"] if a["name"].lower() == "the beatles"]
    assert beatles == ["The Beatles"], beatles


def test_a_compilation_is_one_album_under_its_album_artist(got):
    nd = _album(got, "Night Drive")
    assert nd["artist"] == "Various Artists" and nd["tracks"] == ["c2", "c1"], nd


def test_the_same_title_by_two_artists_is_two_albums(got):
    assert _album(got, "Greatest Hits", "Band X")["tracks"] == ["g1"]
    assert _album(got, "Greatest Hits", "Band Y")["tracks"] == ["g2"]


def test_untagged_songs_are_kept_under_unknown_and_listed_last(got):
    unknown = [a for a in got["albums"] if a["unknown"]]
    assert unknown, got["albums"]
    every = {s for a in got["albums"] for s in a["tracks"]}
    assert every == {x["sha"] for x in LIBRARY}, ("a song went missing from Albums", every)
    assert all(a["unknown"] for a in got["albums"][-len(unknown):]), ("unknown albums must sort last", got["albums"])
    assert {"u1", "u2"} <= {s for a in unknown for s in a["tracks"]}
    assert any(a["album"] == "Unknown album" for a in unknown)
    # A title-only track and a file with nothing at all are both still songs.
    assert {"u1", "u2", "u3"} <= set(got["sorts"]["added"])


def test_artists_hold_their_albums_and_songs_in_order(got):
    names = [a["name"] for a in got["artists"]]
    assert names[-1] == "Unknown artist", names
    beatles = [a for a in got["artists"] if a["name"].lower() == "the beatles"]
    assert len(beatles) == 1, ("the same artist in two cases became two artists", names)
    assert beatles[0]["tracks"] == ["ab3", "ab1", "ab5", "ab9"], beatles[0]
    va = [a for a in got["artists"] if a["name"] == "Various Artists"][0]
    assert va["albums"] == ["Night Drive"], va
    solo = [a for a in got["artists"] if a["name"] == "Solo Act"][0]
    assert solo["albums"] == ["Unknown album"] and solo["tracks"] == ["u3"], solo


def test_songs_sort_by_title_artist_album_and_recently_added(got):
    s = got["sorts"]
    assert s["added"][:3] == ["u3", "u2", "ab1"], s["added"]            # newest upload first
    assert s["title"][0] == "ab3" and s["title"].index("ab5") < s["title"].index("ab1"), s["title"]  # Come.., Octopus, Something
    # By artist: Band X, Band Y, Solo Act, The Beatles (album then track), Various Artists... untagged last.
    art = s["artist"]
    assert art.index("g1") < art.index("g2") < art.index("u3") < art.index("ab3") < art.index("c2"), art
    assert art[-2:] == ["u1", "u2"] or set(art[-2:]) == {"u1", "u2"}, art
    assert art.index("ab3") < art.index("ab1") < art.index("ab5") < art.index("ab9"), art
    # By album: alphabetical by album, track order inside, untagged albums last.
    alb = s["album"]
    assert alb.index("ab3") < alb.index("g1") < alb.index("c2") < alb.index("c1"), alb
    assert set(alb[-3:]) == {"u1", "u2", "u3"}, alb


def test_one_search_box_filters_each_tab_by_what_it_lists(got):
    assert set(got["search"]["song"]) == {"c1", "c2"}, got["search"]          # "night" -> the album Night Drive
    assert got["search"]["album"] and all("abbey" in a.lower() for a in got["search"]["album"]), got["search"]
    assert got["search"]["artist"] == ["The Beatles"], got["search"]
