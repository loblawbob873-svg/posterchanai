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


def test_publishing_waits_for_a_relay_socket():
    """"I clicked sync and nothing happened."

    publishAndWait resolves FALSE the instant no socket is open — and none is, in the moment a service
    worker (or a just-woken event page) starts handling the popup's message: the relay connection is
    still being established. So enabling sync ran the whole merge against a closed socket and published
    nothing, whatever the pairing mode. It waits for one now, and retries on EOSE when a relay actually
    answers, because a merge that silently sends nothing is indistinguishable from a broken feature.
    """
    src = open(os.path.join(ROOT, "extension", "background.js"), encoding="utf-8").read()
    i = src.index("async function publishBookmark(")
    body = src[i:i + 700]
    assert "await waitOpen()" in body, \
        "publishBookmark does not wait for a socket; the first merge after a wake sends nothing"
    assert "function waitOpen(" in src
    # And the retry, so a failure while offline is not permanent until the user clicks again.
    j = src.index("'EOSE'")
    assert "BM.engine.union()" in src[j:j + 600], \
        "nothing retries the unpublished bookmarks when a relay finally answers"


def test_bookmarks_are_sealed_and_uncleanable():
    """The two questions worth asking of anything new that lives on a relay: is it encrypted, and can
    a cleaner delete it?

    ENCRYPTED — the body goes through the vault's `seal`, AES-256-GCM under the 32-byte vault key with
    a random IV, the same call a password uses. The relay holds ciphertext and cannot read a URL.

    UNCLEANABLE — bookmarks are kind 30078, which is in the relay's `_NEVER_EXPIRE_KINDS`. That is not
    incidental: NIP-37 recommends stamping drafts with `expiration: now+90d`, and the NIP-40 sweep is
    otherwise unconditional, so any other client touching one of these events could have made it
    vanish 90 days later from the relay holding the only copy. The tag is DROPPED at ingest rather
    than merely un-swept (a stored expiration hides the event from every read), and the same kinds are
    excluded from prune, with an assert that the two sets cannot overlap. Notes learned this the hard
    way; bookmarks inherit it by being the same kind.
    """
    bg = open(os.path.join(ROOT, "extension", "background.js"), encoding="utf-8").read()
    i = bg.index("async function publishBookmark(")
    body = bg[i:i + 900]
    assert "V.seal(key, item)" in body, "bookmark bodies must be sealed, not published in the clear"
    assert "kind: KIND" in body, "bookmarks must publish under kind 30078"
    assert "const KIND = 30078;" in bg

    store = open(os.path.join(ROOT, "app", "services", "nostr_relay", "store.py"), encoding="utf-8").read()
    assert "_NEVER_EXPIRE_KINDS = _GIT_KINDS + (30078,)" in store, \
        "kind 30078 lost its expiry exemption — every bookmark, note and setting becomes deletable " \
        "by a stray NIP-40 tag from any other client"
    assert 'assert not (set(_NEVER_EXPIRE_KINDS) & set(_PRUNABLE_KINDS))' in store, \
        "the never-expire/prunable overlap guard is gone"


def test_relays_are_user_definable_with_a_working_default():
    """The pairing code carries a snapshot of whatever the app's relay list was at that moment. If it
    carried an unreachable relay — or none — the extension can never publish, and the failure is
    invisible: nothing syncs and nothing says why. That is precisely what "I clicked sync and nothing
    happened" looks like from inside the browser, and no amount of querying relays from outside can
    tell it apart from a read-only pairing.

    So the list is editable, and there is a default to fall back to rather than an empty set."""
    bg = open(os.path.join(ROOT, "extension", "background.js"), encoding="utf-8").read()
    assert "const DEFAULT_RELAY = 'wss://relay.poster.place'" in bg, \
        "no default relay — a pairing code without one leaves the extension with nothing to talk to"
    i = bg.index("function relayUrls()")
    body = bg[i:i + 500]
    assert "userRelays" in body and "DEFAULT_RELAY" in body, \
        "relayUrls must prefer the user's list and fall back to the default"
    assert "case 'relays-set'" in bg and "case 'relays-get'" in bg

    pj = open(os.path.join(ROOT, "extension", "popup.js"), encoding="utf-8").read()
    assert "type:'relays-set'" in pj, "the popup cannot save a relay list"


def test_a_typo_is_dropped_not_stored():
    """A relay list that silently keeps 'my relay' as an entry is a list that looks set and is not."""
    got = subprocess.run(["node", "-e", """
      const fs = require('fs');
      const src = fs.readFileSync(process.argv[1], 'utf8');
      const m = src.match(/function normRelay\\(u\\)\\{[\\s\\S]*?\\n\\}/);
      const f = new Function('return ' + m[0])();
      console.log(JSON.stringify(['relay.poster.place','https://r.example','not a relay','',
                                  'ws://localhost:3052'].map(f)));
    """, os.path.join(ROOT, "extension", "background.js")], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout.strip())
    assert out[0] == "wss://relay.poster.place", "a bare host must become a wss:// URL"
    assert out[1] == "wss://r.example", "https must become wss"
    assert out[2] == "" and out[3] == "", "junk must be dropped"
    assert out[4] == "ws://localhost:3052", "a local relay must survive"


def _tree_harness(body):
    """A fake bookmarks API that behaves like a real one — create/move/remove are ASYNC — so a
    check-then-create without a lock races exactly as the browser's does."""
    return run("""
      let nid = 100;
      const nodes = { root:{id:'root',children:['1','2']},
                      '1':{id:'1',title:'Bookmarks bar',parentId:'root',children:[]},
                      '2':{id:'2',title:'Other bookmarks',parentId:'root',children:[]} };
      const mk = (parent,title,url) => { const n={id:String(++nid),title,url,parentId:parent,children:[]};
        nodes[n.id]=n; nodes[parent].children.push(n.id); return n; };
      const tick = () => new Promise(r=>setTimeout(r,1));
      const B = { storage:{local:{get:async()=>({bmOn:true}),set:async()=>{}}},
        bookmarks:{ onCreated:{addListener(){}},onChanged:{addListener(){}},
          onMoved:{addListener(){}},onRemoved:{addListener(){}},
          getTree: async()=>{ await tick(); return [{id:'root',children:[nodes['1'],nodes['2']]}]; },
          getChildren: async(id)=>{ await tick(); return (nodes[id].children||[]).map(c=>nodes[c]); },
          create: async(o)=>{ await tick(); return mk(o.parentId,o.title,o.url); },
          get: async(id)=>[nodes[id]],
          move: async(id,o)=>{ await tick(); const n=nodes[id];
            nodes[n.parentId].children = nodes[n.parentId].children.filter(c=>c!==id);
            n.parentId=o.parentId; nodes[o.parentId].children.push(id); },
          remove: async(id)=>{ await tick(); const n=nodes[id];
            nodes[n.parentId].children = nodes[n.parentId].children.filter(c=>c!==id); delete nodes[id]; },
          update: async()=>{} } };
      const E = P.engine;
      (async () => {
      """ + body + """
      })();
    """)


def test_a_burst_of_arrivals_creates_one_folder_not_twenty():
    """THE duplicate-folder bug, reported as "I see a bunch of dupe folders" after a first sync.

    Events arrive from the subscription in a burst and each is applied independently, so twenty
    bookmarks in one folder all ran the folder lookup at once: every one called getChildren, none saw
    the folder because none had been created yet, and every one created its own. Check-then-create is
    not atomic. Measured against this harness before the fix: 20 "Work" folders and 20 "News" ones."""
    got = _tree_harness("""
      await E.init({ B, open: async(ct)=>JSON.parse(ct), publish: async()=>true,
                     isFull: ()=>true, why: ()=>'' });
      const evs = Array.from({length:20}, (_,i) => ({ created_at:1000+i,
        content: JSON.stringify({ title:'B'+i, url:'https://x'+i+'.example/',
                                  folder:'Work/News', root:'toolbar' }) }));
      await Promise.all(evs.map((ev,i) => E.absorb('id'+i, ev)));
      out({ work: Object.values(nodes).filter(n=>!n.url&&n.title==='Work').length,
            news: Object.values(nodes).filter(n=>!n.url&&n.title==='News').length,
            marks: Object.values(nodes).filter(n=>n.url).length });
    """)
    assert got["work"] == 1, f"{got['work']} copies of the folder — the create race is back"
    assert got["news"] == 1, f"{got['news']} copies of the nested folder"
    assert got["marks"] == 20, "every bookmark must still be created"


def test_tidy_merges_duplicates_without_losing_a_bookmark():
    """Cleanup for the duplicates an earlier build already made. It may only MOVE children and delete
    a folder that is empty afterwards — never a bookmark, in any branch."""
    got = _tree_harness("""
      for (let i=0;i<5;i++){ const f=mk('1','Work'); mk(f.id,'B'+i,'https://x'+i+'.example/'); }
      await E.init({ B, open: async(ct)=>JSON.parse(ct), publish: async()=>true,
                     isFull: ()=>true, why: ()=>'' });
      const before = Object.values(nodes).filter(n=>!n.url&&n.title==='Work').length;
      const r = await E.tidy();
      out({ before, after: Object.values(nodes).filter(n=>!n.url&&n.title==='Work').length,
            marks: Object.values(nodes).filter(n=>n.url).length, r });
    """)
    assert got["before"] == 5
    assert got["after"] == 1, f"tidy left {got['after']} copies"
    assert got["marks"] == 5, "tidy deleted a bookmark — it may only move them"
    assert got["r"]["merged"] == 4 and got["r"]["removed"] == 4
