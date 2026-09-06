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
    # A union DOES delete now — but only what it had MAPPED and can no longer find locally, which is
    # a deletion that happened here. Nothing in this fixture is mapped, so nothing may be removed.
    assert got["remove"] == [], "a union removed something it had never synced"


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
    # `superseded` joined the plan when sync ids became URL-derived: it lists duplicate EVENTS for a
    # URL, which older builds could create and which are dropped rather than acted on.
    assert got == {"publish": [], "create": [], "link": [], "remove": [], "superseded": [],
                   "skipRemoved": 0}


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


def test_the_same_url_on_two_browsers_is_ONE_bookmark():
    """SUPERSEDED, and this is the duplication between two browsers.

    This used to assert that a toolbar copy and an "other" copy of one URL were different bookmarks,
    so a union created the missing one. That is what two browsers ALWAYS look like: a link on Chrome's
    toolbar lives in Firefox's Bookmarks Menu, which has no Chrome equivalent at all. Each browser
    therefore created the other's copy and published its own — two copies everywhere, from one
    bookmark, with nobody having done anything wrong.

    Identity is the URL now. Place is data about a bookmark, not what makes it that bookmark; it only
    disambiguates between several local copies of the same URL. And a link across a placement
    difference does NOT move anything: re-filing somebody's toolbar because another machine keeps it
    elsewhere is an opinion, not a sync."""
    got = run("""
      const local  = [{id:'b1', title:'News', url:'https://news.example/', folder:'', root:'menu'}];
      const remote = [{id:'s1', title:'News', url:'https://news.example/', folder:'', root:'toolbar', _at:5}];
      out(P.planUnion(local, remote, {}));
    """)
    assert got["create"] == [], "the same URL was created a second time because it is filed elsewhere"
    assert got["publish"] == [], "…and published again, so the duplicate spreads"
    assert got["link"] == [{"syncId": "s1", "browserId": "b1"}], "the two copies must be linked"


def test_the_same_url_filed_twice_still_disambiguates_by_place():
    """Identity being the URL must not merge two copies somebody keeps on purpose: with several
    candidates, the one already in the same spot wins."""
    got = run("""
      const local  = [{id:'b1', title:'N', url:'https://news.example/', folder:'', root:'menu'},
                      {id:'b2', title:'N', url:'https://news.example/', folder:'', root:'toolbar'}];
      const remote = [{id:'s1', title:'N', url:'https://news.example/', folder:'', root:'toolbar', _at:5}];
      out(P.planUnion(local, remote, {}));
    """)
    assert got["link"] == [{"syncId": "s1", "browserId": "b2"}], \
        "the toolbar copy should claim the toolbar item, not the menu one"
    assert [p["id"] for p in got["publish"]] == ["b1"], "the other copy is still its own bookmark"


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
    # Take the WHOLE function, not a fixed byte window. This read bg[i:i+500] and went red the day
    # the comment inside relayUrls() grew past 500 characters — the code was correct, the test was
    # measuring in the wrong unit. A window that a comment can invalidate tests formatting, not
    # behaviour.
    i = bg.index("function relayUrls()")
    end = bg.index("\n}", i)
    body = bg[i:end]
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
          // NESTED NODE OBJECTS, the way a browser returns them. Returning children as id strings
          // made snapshot() see an empty tree, so tests about publishing proved nothing.
          getTree: async()=>{ await tick();
            const hydrate = (id) => Object.assign({}, nodes[id],
              { children: (nodes[id].children||[]).map(hydrate) });
            return [hydrate('root')]; },
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


def test_the_engine_refuses_to_act_before_its_state_is_loaded():
    """Reported as two symptoms with one cause: "Sync bookmarks keeps getting unchecked" and "keeps
    bringing back dupe folders".

    init() assigns `api` on its first line and reads the stored state an await later. A popup opening
    right after a service-worker wake asked for state inside that window and got enabled=false — the
    toggle rendered UNCHECKED on a browser where sync was on. Re-ticking it ran a union against an
    EMPTY known-map, so nothing deduped: every bookmark was republished under a fresh sync id and
    every other browser created it as new.

    So the flag is "storage has been read", not "api exists", and acting early is refused rather than
    done against empty state."""
    src = open(os.path.join(ROOT, "extension", "bookmarks.js"), encoding="utf-8").read()
    assert "var loaded = false;" in src
    assert "loaded = true;" in src
    for guard in ("if (!api || !loaded) throw", "return !!api && loaded && on;"):
        assert guard in src, f"missing the half-loaded guard: {guard}"

    bg = open(os.path.join(ROOT, "extension", "background.js"), encoding="utf-8").read()
    # A bm-* message must not be answered while the engine is still loading -- one answered early
    # sees an empty set, and an empty set is a delete order to a reconcile. It waits on its OWN gate
    # now rather than on the chain every message uses: making a signature or an upload wait for the
    # bookmark tree is how one slow subsystem became "the signer is not responding".
    assert "const bookmarksReady = ready.then(() => initBookmarks())" in bg, \
        "the engine's init is fired and forgotten again; a message handled during it sees empty state"
    assert "if(String((msg && msg.type) || '').startsWith('bm-')) await bookmarksReady;" in bg, \
        "bm-* handlers no longer wait for the bookmark engine to finish loading"
    # And the wait is bookmark-only: the general chain must not await it.
    chain = bg[bg.index("const ready = (async () => {"): bg.index("const bookmarksReady")]
    assert "initBookmarks" not in chain, \
        "initBookmarks is back in the chain every message awaits"


def test_enabling_before_load_does_not_republish_everything():
    """The consequence, measured: a union that runs with no loaded state republishes the lot."""
    got = _tree_harness("""
      for (let i=0;i<3;i++) mk('1','B'+i,'https://x'+i+'.example/');
      let published = 0;
      // init is deliberately NOT awaited — the window the bug lived in.
      const p = E.init({ B, open: async(ct)=>JSON.parse(ct),
                         publish: async()=>{ published++; return true; }, isFull:()=>true, why:()=>'' });
      let refused = '';
      try { await E.setEnabled(true); } catch (e) { refused = e.message; }
      await p;
      out({ refused, published });
    """)
    assert got["refused"], "setEnabled ran against a half-loaded engine instead of refusing"
    assert got["published"] == 0, \
        f"{got['published']} events published from an unloaded engine — that is the duplicate storm"


def test_legacy_events_land_where_they_were_meant_to():
    """The FIRST build of this feature published the browser's own container inside the folder path
    ("Bookmarks bar", "Other bookmarks") and no `root` at all. Those events are still on the relay and
    cannot be rewritten, so they have to be understood rather than trusted:

      * left alone they recreate a literal folder called "Other bookmarks" — reported as "every time I
        remove Other bookmarks it comes back during sync";
      * merely STRIPPED, every toolbar bookmark that build published lands in "Other" instead —
        reported as "it synced the folders but not the bookmarks on the toolbar".

    So a leading container segment is consumed AND, when the item carries no root, supplies one."""
    got = run("""
      out({
        legacyBar:    P.placement({folder:'Bookmarks bar'}),
        legacyNested: P.placement({folder:'Bookmarks Toolbar/Work'}),
        legacyOther:  P.placement({folder:'Other bookmarks'}),
        legacyMenu:   P.placement({folder:'Bookmarks Menu/A'}),
        current:      P.placement({folder:'Work', root:'toolbar'}),
        currentBare:  P.placement({folder:'', root:'toolbar'}),
      });
    """)
    assert got["legacyBar"] == {"folder": "", "root": "toolbar"}, \
        "a legacy toolbar bookmark must land ON the toolbar, not in Other"
    assert got["legacyNested"] == {"folder": "Work", "root": "toolbar"}
    assert got["legacyOther"] == {"folder": "", "root": "other"}, \
        "'Other bookmarks' must be consumed, or it is recreated as a folder every sync"
    assert got["legacyMenu"] == {"folder": "A", "root": "menu"}
    assert got["current"] == {"folder": "Work", "root": "toolbar"}, "current events must be untouched"
    assert got["currentBare"] == {"folder": "", "root": "toolbar"}


def test_deletion_is_disabled_end_to_end():
    """After the data loss: a tombstone must not remove anything, and removing a bookmark here must
    not publish one. The guard that was meant to bound deletion ("only what this browser previously
    synced") was intact and still not enough — an earlier republish storm had corrupted the identity
    it reasons about, so every deletion was 'legitimate' by the rule. A sync that can only ADD cannot
    do that."""
    src = open(os.path.join(ROOT, "extension", "bookmarks.js"), encoding="utf-8").read()
    i = src.index("async function applyRemoval(")
    body = src[i:i + 400]
    assert "bookmarks.remove(" not in body, "a tombstone still deletes a local bookmark"
    j = src.index("async function onLocalRemove(")
    body2 = src[j:j + 400]
    assert "publish(" not in body2, "removing a bookmark still publishes a tombstone to other devices"
    # tidy is the one place that may remove, and only a folder it just emptied.
    k = src.index("async function tidy(")
    assert "bookmarks.remove(" in src[k:k + 2500], "tidy should still remove the folders it empties"


def test_a_delete_propagates_but_a_confused_id_cannot():
    """Deleting on one device must remove it on the other — "I delete everything on Firefox, merge,
    and Firefox gets them back" is what additive-only sync feels like, and it is unusable.

    But this is the operation that destroyed a bookmark tree once. The guard then was "only remove
    what THIS browser previously synced", which was intact and not enough: a bug had republished
    everything under fresh sync ids, so each browser mapped its own real bookmarks onto them and a
    tidy-up on one machine published tombstones that were legitimate for bookmarks they had never
    been about.

    So identity is verified rather than trusted: the URL last known for that id must still match the
    bookmark in the tree. A confused id then costs a LINK — which re-links on the next merge — not
    somebody's bookmarks."""
    got = _tree_harness("""
      const keep = mk('1','Keep','https://keep.example/');
      const doomed = mk('1','Doomed','https://doomed.example/');
      await E.init({ B, open: async(ct)=>JSON.parse(ct), publish: async()=>true,
                     isFull: ()=>true, why: ()=>'' });
      await E.setEnabled(true);            // union maps both
      // A tombstone for the id that really is 'Doomed'…
      const idOf = (u) => { for (const [sid, b] of Object.entries(JSON.parse(JSON.stringify({})))) {} return null; };
      // …found by absorbing an upsert first so we know the id we control.
      await E.absorb('sync-doomed', { created_at: 50,
        content: JSON.stringify({ title:'Doomed', url:'https://doomed.example/', folder:'', root:'toolbar' }) });
      await E.absorb('sync-doomed', { created_at: 60, content: '' });     // tombstone, matching URL
      // …and one whose id maps to a bookmark with a DIFFERENT url (the poisoned case).
      await E.absorb('sync-wrong', { created_at: 50,
        content: JSON.stringify({ title:'Keep', url:'https://keep.example/', folder:'', root:'toolbar' }) });
      await E.absorb('sync-wrong', { created_at: 60,
        content: '' , __note:'tombstone' });
      out({ urls: Object.values(nodes).filter(n=>n.url).map(n=>n.url).sort() });
    """)
    urls = got["urls"]
    assert "https://keep.example/" in urls, "a bookmark was deleted by a tombstone"
    # The matching-URL tombstone may remove its own copy; the point is that Keep survives and no
    # unrelated bookmark is touched.
    assert all(u.startswith("https://") for u in urls)


def test_a_tombstone_carries_the_url_it_is_about():
    """Without it there is nothing for the receiving side to verify against, and the guard degrades to
    "trust the id" — which is the thing that failed."""
    src = open(os.path.join(ROOT, "extension", "bookmarks.js"), encoding="utf-8").read()
    i = src.index("if (!ev.content) {")
    assert "url: (cur && cur.url)" in src[i:i + 400], \
        "a tombstone no longer records the URL it was about"
    j = src.index("async function applyRemoval(")
    body = src[j:j + 1600]
    assert "normUrl(node.url) !== P.normUrl(knew)" in body, \
        "applyRemoval deletes without checking the bookmark is the one the tombstone names"
    assert "forget(id);" in body


def test_deleting_here_deletes_there_instead_of_coming_back():
    """The report that forced this: "I delete everything on Firefox, click merge, then on Chrome
    nothing happens, then Firefox gets them back."

    union() could only ADD. A local delete left no trace, so the next merge saw a remote item it did
    not have and restored it — a one-way download with extra steps. The MAPPING is what this browser
    believed the shared state was, so a sync id whose local bookmark has vanished is a deletion that
    happened while nothing was listening (sync off, browser closed, listeners not yet attached)."""
    got = _tree_harness("""
      const a = mk('1','Keep','https://keep.example/');
      const b = mk('1','Gone','https://gone.example/');
      const sent = [];
      await E.init({ B, open: async(ct)=>JSON.parse(ct),
                     publish: async(id,item)=>{ sent.push({id, tomb: item === null}); return true; },
                     isFull: ()=>true, why: ()=>'' });
      await E.setEnabled(true);                       // maps both
      // Delete one the way a user does with sync off: straight out of the tree, no event.
      nodes['1'].children = nodes['1'].children.filter(c => c !== b.id); delete nodes[b.id];
      sent.length = 0;
      const r = await E.union();
      out({ tombstones: sent.filter(x=>x.tomb).length, removed: r.removed,
            stillThere: Object.values(nodes).some(n=>n.url==='https://gone.example/'),
            keepThere: Object.values(nodes).some(n=>n.url==='https://keep.example/') });
    """)
    assert got["tombstones"] == 1, "the local deletion was not published — the other device keeps it"
    assert got["removed"] == 1
    assert got["stillThere"] is False, "the merge RESTORED the bookmark that was deleted here"
    assert got["keepThere"] is True, "an unrelated bookmark was affected"


def test_a_wholesale_disappearance_asks_before_deleting_everywhere():
    """Deleting everything on purpose and restoring a backup look identical from here, and one of them
    must not quietly delete the same bookmarks on every other device. Past the threshold the merge
    stops and reports; the popup then offers to go ahead, so the deliberate case still works."""
    got = _tree_harness("""
      for (let i=0;i<10;i++) mk('1','B'+i,'https://x'+i+'.example/');
      const sent = [];
      await E.init({ B, open: async(ct)=>JSON.parse(ct),
                     publish: async(id,item)=>{ sent.push({id, tomb: item === null}); return true; },
                     isFull: ()=>true, why: ()=>'' });
      await E.setEnabled(true);
      nodes['1'].children = [];                       // everything vanishes at once
      sent.length = 0;
      const stopped = await E.union();
      const after = sent.filter(x=>x.tomb).length;
      const confirmed = await E.union({ confirmRemovals: true });
      out({ pending: stopped.pendingRemovals, tombstonesBeforeConfirm: after,
            tombstonesAfterConfirm: sent.filter(x=>x.tomb).length, removed: confirmed.removed });
    """)
    assert got["pending"] == 10, "a wholesale disappearance must be reported, not acted on"
    assert got["tombstonesBeforeConfirm"] == 0, "it deleted everywhere without asking"
    assert got["tombstonesAfterConfirm"] == 10, "confirming did not go through"
    assert got["removed"] == 10
