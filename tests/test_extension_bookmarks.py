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


def test_the_toolbar_stays_the_toolbar():
    """A bookmark on the toolbar belongs on the toolbar in the other browser. Dumping everything into
    "other bookmarks" loses the arrangement that made syncing worth doing.

    Classified by root ID first — Chrome '1'/'2'/'3', Firefox 'toolbar_____' etc. — because titles are
    localised ("Lesezeichen-Symbolleiste") and renameable."""
    got = run("""
      out({
        chromeBar:   P.classifyRoot({id:'1', title:'Bookmarks bar'}),
        chromeOther: P.classifyRoot({id:'2', title:'Other bookmarks'}),
        ffToolbar:   P.classifyRoot({id:'toolbar_____', title:'Bookmarks Toolbar'}),
        ffMenu:      P.classifyRoot({id:'menu________', title:'Bookmarks Menu'}),
        ffOther:     P.classifyRoot({id:'unfiled_____', title:'Other Bookmarks'}),
        localised:   P.classifyRoot({id:'weird', title:'Lesezeichen-Symbolleiste'}),
        unknown:     P.classifyRoot({id:'zzz', title:'Something else'}),
      });
    """)
    assert got["chromeBar"] == "toolbar" and got["ffToolbar"] == "toolbar"
    assert got["chromeOther"] == "other" and got["ffOther"] == "other"
    assert got["ffMenu"] == "menu", "Firefox's menu is its own container; Chrome has none"
    assert got["unknown"] == "other", "an unrecognised container must not become the toolbar"
    # A localised toolbar title is not required to be recognised (the ID does that job); this only
    # records what the fallback happens to do.
    assert got["localised"] in ("toolbar", "other")


def test_root_is_read_from_the_tree():
    got = run("""
      // Keyed BY ID — that is what nodesById means, and keying it by a variable name made the
      // lookup miss and the answer fall back to 'other'.
      const byId = { 'root':{id:'root'}, '1':{id:'1',title:'Bookmarks bar',parentId:'root'},
                     'f':{id:'f',title:'News',parentId:'1'} };
      out({ direct: P.rootOf(byId, {id:'x', parentId:'1', url:'https://a.example/'}),
            nested: P.rootOf(byId, {id:'y', parentId:'f', url:'https://b.example/'}) });
    """)
    assert got == {"direct": "toolbar", "nested": "toolbar"}, \
        "a bookmark inside a folder on the toolbar is still on the toolbar"


def test_the_same_url_in_two_containers_is_two_bookmarks():
    """Merging them would silently MOVE one. The root is part of identity for that reason."""
    got = run("""
      const a = {url:'https://a.example/', folder:'', root:'toolbar'};
      const b = {url:'https://a.example/', folder:'', root:'other'};
      out({ same: P.matchKey(a) === P.matchKey(b), a: P.matchKey(a) });
    """)
    assert got["same"] is False, "a toolbar bookmark and an 'other' one collided in the match key"


def test_a_union_pairs_only_within_the_same_container():
    got = run("""
      const local  = [{id:'b1', title:'T', url:'https://a.example/', folder:'', root:'other'}];
      const remote = [{id:'s1', title:'T', url:'https://a.example/', folder:'', root:'toolbar', _at:5}];
      out(P.planUnion(local, remote, {}));
    """)
    assert [c["id"] for c in got["create"]] == ["s1"], \
        "the toolbar copy should be created; it is not the same bookmark as the one in 'other'"
    assert [p["id"] for p in got["publish"]] == ["b1"]
