"""Bookmark sync's decision logic — the half that can destroy data.

Bookmarks sync as one encrypted event per bookmark, exactly like the vault: kind 30078,
`d = pcai:bm:<id>`, body sealed with the same vault key, newest-created_at-wins, empty content is a
tombstone. The crypto and the relay plumbing are the vault's and are already covered; what is NEW and
worth testing on its own is the merge, because it is the part that can delete somebody's bookmarks.

The rules it must obey, each of which is a way to lose data:

  union-never-deletes   The first sync after enabling — and any rebuild — gains bookmarks on both
                        sides and removes NONE. Two devices with different sets must converge by
                        gaining each other's, never by one wiping the other.
  tombstone-is-explicit "I don't have it" is not "it was deleted". Only an explicit tombstone may
                        remove anything, and a union ignores tombstones entirely.
  match-on-url-not-title A rebuilt mapping re-pairs by URL within a folder. Titles get renamed far
                        more often than bookmarks get re-filed; matching on them would duplicate
                        every bookmark somebody tidied up.
  no-executable-urls    javascript: bookmarklets and browser-internal place:/about: entries are not
                        synced. A bookmarklet is executable content arriving from the network.

Run under node against the shipped file, so this cannot drift from what the extension loads.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BM = os.path.join(ROOT, "extension", "bookmarks.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run(body):
    script = f"""
      globalThis.self = globalThis;
      require({json.dumps(BM)});
      const P = globalThis.PCBookmarks;
      const out = (v) => console.log('OUT<<<' + JSON.stringify(v) + '>>>');
      {body}
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.split("OUT<<<")[1].split(">>>")[0])


def test_a_union_never_deletes_anything():
    got = run("""
      const local  = [{id:'b1', title:'Local only', url:'https://a.example/', folder:'Work'}];
      const remote = [{id:'s1', title:'Remote only', url:'https://b.example/', folder:'Work', _at:9},
                      {id:'s2', title:'Deleted elsewhere', url:'https://c.example/', folder:'', _at:9, removed:true}];
      out(P.planUnion(local, remote, {}));
    """)
    assert [c["id"] for c in got["create"]] == ["s1"], "the remote-only bookmark should be created here"
    assert [p["id"] for p in got["publish"]] == ["b1"], "the local-only bookmark should be published"
    assert got["skipRemoved"] == 1, "a tombstone must be IGNORED by a union, not applied"
    assert "delete" not in got and "remove" not in got, "a union produced a delete plan"


def test_the_same_bookmark_on_both_sides_is_paired_not_duplicated():
    got = run("""
      const local  = [{id:'b9', title:'Renamed locally', url:'https://a.example/', folder:'Work'}];
      const remote = [{id:'s9', title:'Original name',  url:'https://a.example/', folder:'Work', _at:5}];
      out(P.planUnion(local, remote, {}));
    """)
    assert got["create"] == [], "it exists here already — creating it would duplicate it"
    assert got["publish"] == [], "it exists remotely already"
    assert got["link"] == [{"syncId": "s9", "browserId": "b9"}], "the two copies must be paired"


def test_a_mapped_bookmark_is_left_alone():
    got = run("""
      const local  = [{id:'b1', title:'T', url:'https://a.example/', folder:''}];
      const remote = [{id:'s1', title:'T', url:'https://a.example/', folder:'', _at:5}];
      out(P.planUnion(local, remote, {s1:'b1'}));
    """)
    assert got == {"publish": [], "create": [], "link": [], "skipRemoved": 0}


def test_only_web_urls_sync():
    got = run("""
      out([
        P.isSyncable({url:'https://ok.example/'}),
        P.isSyncable({url:'http://ok.example/'}),
        P.isSyncable({url:'javascript:alert(1)'}),
        P.isSyncable({url:'place:type=6&sort=14'}),
        P.isSyncable({url:'about:config'}),
        P.isSyncable({title:'a folder'}),
      ]);
    """)
    assert got == [True, True, False, False, False, False], \
        "a javascript: bookmarklet is executable content arriving from a relay"


def test_folder_paths_are_names_not_local_ids():
    got = run("""
      const byId = { root:{id:'root',title:'root'}, m:{id:'m',title:'Bookmarks Menu',parentId:'root'},
                     w:{id:'w',title:'Work',parentId:'m'}, s:{id:'s',title:'Sub',parentId:'w'} };
      out(P.pathOf(byId, {id:'x', parentId:'s', url:'https://a.example/'}));
    """)
    assert got == "Work/Sub", f"got {got!r} — a path of names is what the other browser can recreate"


def test_a_trailing_slash_is_not_a_different_bookmark():
    got = run("""out([P.normUrl('https://a.example'), P.normUrl('https://a.example/'),
                       P.normUrl('https://a.example/p/'), P.normUrl('https://a.example/?q=1')]);""")
    assert got[0] == got[1], "origin with and without a trailing slash must match"
    assert got[2] != got[3], "different paths/queries must NOT be merged"


def test_newest_wins():
    got = run("""out([P.newer({v:'old',_at:1},{v:'new',_at:2}).v,
                      P.newer({v:'new',_at:2},{v:'old',_at:1}).v,
                      P.newer(null,{v:'only',_at:0}).v]);""")
    assert got == ["new", "new", "only"]
