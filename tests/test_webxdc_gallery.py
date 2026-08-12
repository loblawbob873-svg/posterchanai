"""Games → Webxdc: the directory of mini apps, as the CLIENT builds it.

    venv-unified/bin/python -m unittest tests.test_webxdc_gallery

A mini app arrives as an ATTACHMENT to somebody's post, so before this screen the only way to find
one was to scroll past it — and a game nobody can find has no second player, which is most of the
point of the feature. The directory is what makes them findable, and every way it can be wrong is
quiet: a tile that never appears, two rooms collapsed into one so half the players join the wrong
game, or a relay round trip fired every time a desktop window is clicked.

What is asserted here, and why each one is a decision rather than an accident:

  * THREE SOURCES, because no single one sees everything. kind 1063 `#m` (Ditto's shape — the rich
    one, with cover art) and kind 1 `#t webxdc` are indexed and can be ASKED for; the local cache is
    the only thing that can see a post made before the hashtag existed.
  * ONE TILE PER IDENTIFIER. Two people posting the same .xdc share a sha and differ in identifier,
    and the identifier decides whose game you join — so they are two rooms. Merging by file would
    silently drop everyone into whichever sorted first.
  * FIELDS MERGE, records do not. The same app can arrive bare from one post and fully described
    from another; taking either wholesale throws away half of what is known about it.
  * ENTERING IS NOT REFRESHING. renderView runs on every entry AND every desktop-window focus.
  * The .xdc ARCHIVE CACHE must survive a deploy — see SwCacheKeepList below, which is a real bug
    this file was written after finding.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBXDC_JS = ROOT / "static" / "js" / "client" / "webxdc.js"
SW_JS = ROOT / "static" / "js" / "client" / "sw.js"
APP_JS = ROOT / "static" / "js" / "client" / "app.js"
TPL = ROOT / "templates" / "client.html"

XDC = "application/x-webxdc"


def f1063(uuid, name, sha="a" * 64, url=None, image="", size=0, pk="aa" * 32, at=1000, content=""):
    tags = [["url", url or f"https://blossom.example.com/{sha}.xdc"], ["m", XDC], ["x", sha],
            ["alt", f"Webxdc app: {name}"], ["webxdc", uuid]]
    if image:
        tags.append(["image", image])
    if size:
        tags.append(["size", str(size)])
    return {"id": uuid + "-1063", "kind": 1063, "pubkey": pk, "created_at": at,
            "content": content, "tags": tags}


def note(uuid, sha="a" * 64, url=None, pk="bb" * 32, at=1000, summary=""):
    im = ["imeta", "url " + (url or f"https://blossom.example.com/{sha}.xdc"), "m " + XDC,
          "x " + sha, "webxdc " + uuid]
    if summary:
        im.append("summary " + summary)
    return {"id": uuid + "-note", "kind": 1, "pubkey": pk, "created_at": at,
            "content": "come play", "tags": [im, ["t", "webxdc"]]}


def upd(uuid, at=2000, n=0):
    return {"id": f"{uuid}-u{n}", "kind": 4932, "pubkey": "cc" * 32, "created_at": at,
            "content": "{}", "tags": [["i", uuid], ["alt", "webxdc update"]]}


def _node(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, timeout=90)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-3000:])
    return json.loads(out.stdout.decode() or "null")


def gallery(net=(), cache=(), updates=(), view="xdc"):
    """Run the SHIPPED galLoad against stub relay + cache, and report what the grid would show.

    `net` is what the relays answer, `cache` what this device already holds, `updates` the kind-4932
    moves. The three are kept separate precisely because the merge across them is the thing being
    tested — a single blob of events could not tell a cache-only app from a networked one.
    """
    boot = f"""
      const NET = {json.dumps(list(net))}, CACHE = {json.dumps(list(cache))};
      const UPD = {json.dumps(list(updates))};
      const match = (evs, f) => evs.filter(e => {{
        if(f.kinds && !f.kinds.includes(e.kind)) return false;
        for(const k of Object.keys(f)){{
          if(k[0] !== '#') continue;
          const t = k.slice(1);
          if(!e.tags.some(x => x[0] === t && f[k].includes(x[1]))) return false;
        }}
        return true;
      }});
      const saved = [];
      global.window = {{
        addEventListener(){{}},
        __PC: {{ VIEW: {json.dumps(view)}, $:()=>null, enc:s=>String(s), toast(){{}}, publish(){{}},
                 me:()=>null, profOf:()=>({{name:'someone'}}), needProfile(){{}}, safePk:p=>p.slice(0,8),
                 decorateProfiles(){{}}, openThread(){{}}, apiBase:()=>'https://example.com' }},
        Store: {{ query: fs => fs.flatMap(f => match(CACHE, f)), saveEvent: e => {{ saved.push(e.id); }} }},
        Relay: {{ query: async (fs) => fs.flatMap(f => f.kinds && f.kinds.includes(4932)
                                                       ? match(UPD, f) : match(NET, f)) }},
      }};
      global.Relay = window.Relay; global.Store = window.Store;
      global.document = {{ addEventListener(){{}}, querySelectorAll:()=>[], getElementById:()=>null,
                          createElement:()=>({{ setAttribute(){{}}, classList:{{add(){{}}}},
                                               appendChild(){{}}, remove(){{}}, style:{{}} }}) }};
      global.location = {{ hostname:'example.com', href:'https://example.com/' }};
      require({json.dumps(str(WEBXDC_JS))});
      (async () => {{
        const W = window.PCWebxdc;
        const apps = await W.__galLoad(true);
        console.log(JSON.stringify({{
          keys: apps.map(a => W.__galKey(a)),
          apps: apps.map(a => ({{ key:W.__galKey(a), name:a.name, image:a.image, size:a.size,
                                 plays:a.plays, pubkey:a.pubkey, evId:a.evId, at:a.at,
                                 url:a.url, sha:a.sha }})),
          saved,
          html: apps.map(a => W.__galTile(a)).join(''),
        }}));
      }})();
    """
    return _node(boot)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Sources(unittest.TestCase):

    def test_ditto_style_1063_apps_are_found(self):
        """The source that matters today — nine real apps reach this client this way."""
        r = gallery(net=[f1063("u1", "Quake III", image="https://x/c.png", size=5825997),
                         f1063("u2", "Space Invaders", sha="b" * 64, size=6609)])
        self.assertEqual(sorted(r["keys"]), ["u1", "u2"])
        by = {a["key"]: a for a in r["apps"]}
        self.assertEqual(by["u1"]["name"], "Quake III")     # the "Webxdc app: " prefix is stripped
        self.assertEqual(by["u1"]["image"], "https://x/c.png")
        self.assertEqual(by["u1"]["size"], 5825997)

    def test_a_hashtagged_post_is_found(self):
        r = gallery(net=[note("u3")])
        self.assertEqual(r["keys"], ["u3"])

    def test_the_cache_finds_a_post_no_query_could(self):
        """A note published before app.js started tagging `t webxdc`: unindexed, so the ONLY way it
        is ever seen again is this scan of what the device already holds."""
        old = note("u4")
        old["tags"] = [t for t in old["tags"] if t[0] != "t"]
        r = gallery(net=[], cache=[old])
        self.assertEqual(r["keys"], ["u4"])

    def test_what_the_network_taught_us_is_kept(self):
        """Otherwise the next visit re-queries for apps this device already learned about."""
        r = gallery(net=[f1063("u5", "Chess")])
        self.assertIn("u5-1063", r["saved"])

    def test_a_post_with_no_app_contributes_nothing(self):
        plain = {"id": "p", "kind": 1, "pubkey": "dd" * 32, "created_at": 5,
                 "content": "hello", "tags": [["t", "webxdc"]]}
        self.assertEqual(gallery(net=[plain])["keys"], [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Identity(unittest.TestCase):

    def test_the_same_file_posted_twice_is_two_rooms(self):
        """THE ONE THAT SENDS PLAYERS TO THE WRONG GAME. Same .xdc, same sha, two identifiers — and
        the identifier is what decides whose game you join, so these are two rooms and two tiles."""
        r = gallery(net=[f1063("room-a", "Chess", sha="c" * 64, pk="11" * 32),
                         f1063("room-b", "Chess", sha="c" * 64, pk="22" * 32)])
        self.assertEqual(sorted(r["keys"]), ["room-a", "room-b"])

    def test_one_app_announced_twice_is_one_tile(self):
        """The same room reaching us as both a 1063 and a note is still one room."""
        r = gallery(net=[f1063("u6", "Chess"), note("u6")])
        self.assertEqual(r["keys"], ["u6"])

    def test_an_app_with_no_identifier_falls_back_to_the_file(self):
        """It cannot be merged with anything (it is a solo copy), but it must still be listed."""
        anon = f1063("", "Loner", sha="e" * 64)
        anon["tags"] = [t for t in anon["tags"] if t[0] != "webxdc"]
        r = gallery(net=[anon])
        self.assertEqual(r["keys"], ["sha:" + "e" * 64])

    def test_a_non_digest_x_tag_is_not_an_identity(self):
        """`x` is only SOMETIMES a sha256 — the published Half-Life port carries the literal "hl".
        Keyed on it raw, two unrelated uuid-less apps collapse to one tile carrying one app's URL
        under the other's name, and the second vanishes from the directory. `_cacheKey` already had
        to learn this after it served the wrong game."""
        apps = []
        for n, u in (("Half-Life", "https://a.example/hl.xdc"), ("Other", "https://b.example/o.xdc")):
            a = f1063("", n, sha="hl", url=u)
            a["id"] = n
            a["tags"] = [t for t in a["tags"] if t[0] != "webxdc"]
            apps.append(a)
        r = gallery(net=apps)
        self.assertEqual(len(r["keys"]), 2, f"two apps collapsed into {r['keys']}")
        self.assertEqual(sorted(r["keys"]),
                         ["sha:https://a.example/hl.xdc", "sha:https://b.example/o.xdc"])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Merging(unittest.TestCase):

    def test_fields_merge_rather_than_records(self):
        """A bare note and a fully described 1063 for the SAME room: the tile keeps the cover art
        from one and stays one tile. Taking either record wholesale loses half of what is known."""
        r = gallery(net=[note("u7", at=50), f1063("u7", "Quake", image="https://x/q.png", size=99, at=900)])
        self.assertEqual(len(r["apps"]), 1)
        a = r["apps"][0]
        self.assertEqual(a["image"], "https://x/q.png")
        self.assertEqual(a["name"], "Quake")
        self.assertEqual(a["size"], 99)

    def test_the_earliest_post_is_the_one_credited(self):
        """That is where the app was published; a later re-share is not its author."""
        r = gallery(net=[f1063("u8", "Chess", pk="99" * 32, at=5000),
                         note("u8", pk="11" * 32, at=100)])
        self.assertEqual(r["apps"][0]["pubkey"], "11" * 32)
        self.assertEqual(r["apps"][0]["at"], 100)

    def test_the_bytes_played_belong_to_the_author_credited(self):
        """THE ONE THAT RUNS SOMEBODY ELSE'S CODE UNDER YOUR NAME.

        Credit moved to the earliest post while `url`/`sha` kept whatever record was INSERTED first —
        and insertion order here is cache-scan order, not post order. So the tile could read "by
        Alice", link to Alice's post, and download and execute the bytes at Bob's URL under the same
        identifier: exactly the move an attacker reposting a popular identifier would make. Identity
        and bytes are now one decision taken from one record."""
        attacker = f1063("shared", "Chess", sha="f" * 64, url="https://evil.example/x.xdc",
                         pk="ee" * 32, at=9000)
        original = f1063("shared", "Chess", sha="d" * 64, url="https://good.example/x.xdc",
                         pk="11" * 32, at=100)
        original["id"] = "orig"
        # The attacker's record is seen FIRST (it is the newer post, and the cache scan is unordered).
        for order in ([attacker, original], [original, attacker]):
            r = gallery(net=order)
            a = r["apps"][0]
            self.assertEqual(a["pubkey"], "11" * 32)
            self.assertEqual(a["url"], "https://good.example/x.xdc",
                             "credited the original author but Play would run the other archive")
            self.assertEqual(a["sha"], "d" * 64, "the hash must travel with the URL it verifies")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Ordering(unittest.TestCase):

    def test_busy_rooms_come_first(self):
        r = gallery(net=[f1063("quiet", "Quiet", sha="1" * 64, at=9000),
                         f1063("busy", "Busy", sha="2" * 64, at=10)],
                    updates=[upd("busy", n=i) for i in range(4)])
        self.assertEqual(r["keys"], ["busy", "quiet"])
        self.assertEqual(r["apps"][0]["plays"], 4)

    def test_an_unplayed_app_still_appears(self):
        """A directory that hides its new arrivals can never get any."""
        r = gallery(net=[f1063("new", "New", sha="3" * 64, at=9000),
                         f1063("old", "Old", sha="4" * 64, at=10)],
                    updates=[upd("old", n=i) for i in range(3)])
        self.assertEqual(r["keys"], ["old", "new"])

    def test_moves_for_an_app_we_have_never_seen_are_ignored(self):
        """A room with no app record is a tile that could not be drawn or played."""
        r = gallery(net=[f1063("known", "Known")], updates=[upd("ghost"), upd("known")])
        self.assertEqual(r["keys"], ["known"])
        self.assertEqual(r["apps"][0]["plays"], 1)

    def test_the_play_counts_are_asked_for_by_identifier(self):
        """Unscoped, the query returns the 500 newest moves on the NETWORK — so a busy stranger's
        game crowds out the counts for everything in this list, and the download grows with the
        network rather than with the directory."""
        src = (ROOT / "static" / "js" / "client" / "webxdc.js").read_text(encoding="utf-8")
        self.assertIn("kinds:[KIND_UPDATE], '#i': ids", src)

    def test_no_apps_means_no_moves_query_at_all(self):
        r = gallery(net=[], updates=[upd("ghost")])
        self.assertEqual(r["keys"], [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Tiles(unittest.TestCase):

    def test_a_tile_carries_its_cover_its_author_and_a_way_in(self):
        r = gallery(net=[f1063("u9", "Quake III", image="https://x/c.png", size=5825997)],
                    updates=[upd("u9")])
        h = r["html"]
        self.assertIn('src="https://x/c.png"', h)
        self.assertIn("Quake III", h)
        self.assertIn("xdc-tplay", h)
        self.assertIn("xdc-tpost", h)
        self.assertIn("1 move", h)          # singular, and the count is the play signal
        self.assertIn("5.6 MB", h)

    def test_a_coverless_app_gets_the_glyph_not_a_broken_image(self):
        h = gallery(net=[f1063("u10", "Plain")])["html"]
        self.assertIn("xdc-cover-none", h)
        self.assertIn("#i-gamepad", h)
        self.assertNotIn("<img", h)

    def test_the_cover_is_lazy_and_survives_a_dead_host(self):
        """These URLs are other people's servers; a 404 must leave a tile, not a broken-image icon."""
        h = gallery(net=[f1063("u11", "X", image="https://x/c.png")])["html"]
        self.assertIn('loading="lazy"', h)
        self.assertIn("this.remove()", h)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Wiring(unittest.TestCase):
    """The parts a stub relay cannot reach."""

    def test_an_empty_result_is_never_cached_as_authoritative(self):
        """A failed load and an empty directory are the same answer from in here: Relay.query
        resolves [] both for a relay that said nothing and for sockets still CONNECTING, which is
        the normal state seconds after launch. Stamped warm, "No mini apps found yet" would repaint
        on every entry and every window focus for 15 minutes without asking again."""
        src = WEBXDC_JS.read_text(encoding="utf-8")
        self.assertIn("at: apps.length ? Date.now() : 0", src)

    def test_every_kind_the_composer_can_attach_an_app_to_is_queried(self):
        """imetaTagsFor emits the hashtag for polls (1068), comments (1111) and git issues (1621)
        too, so asking only for kind 1 tags those posts for discovery and never looks for them."""
        src = WEBXDC_JS.read_text(encoding="utf-8")
        self.assertIn("kinds:[1, 1068, 1111, 1621], '#t':['webxdc']", src)

    def test_a_parked_desktop_window_is_still_painted(self):
        """os.js parks a window by moving its nodes aside and restores it WITHOUT re-rendering. A
        guard of `VIEW === 'xdc'` alone left a gallery parked mid-load on a spinner with a disabled
        Refresh button, for ever. The real question is whether this call's markup is still on
        screen — true for a parked window, false for a view that was replaced."""
        src = WEBXDC_JS.read_text(encoding="utf-8")
        self.assertIn("document.body.contains(feed)", src)
        self.assertIn("!mine()", src)

    def test_the_gallery_is_hidden_without_an_instance(self):
        """The directory would work, but Play cannot: an app runs on `xdc.<instance>`, and with no
        instance the host falls back to the bundle's own origin. A gallery of games that cannot
        start one is worse than no row."""
        app = APP_JS.read_text(encoding="utf-8")
        block = app[app.index("const INSTANCE_VIEWS = new Set("):]
        self.assertIn("'xdc'", block[:block.index("]);")])

    def test_entering_the_view_does_not_re_query(self):
        """renderView runs on entry AND on every desktop-window focus, so a gallery that queried on
        render would hit the relays every time somebody clicked its window."""
        src = WEBXDC_JS.read_text(encoding="utf-8")
        g = src[src.index("    async function gallery(){"):]
        g = g[:g.index("\n    }") + 6]
        self.assertIn("const warm = _gal.at", g)
        self.assertIn("if(warm) return;", g)
        self.assertIn("galLoad(false)", g)
        self.assertNotIn("galLoad(true)", g)      # only the Refresh button forces

    def test_a_running_app_is_brought_forward_not_reloaded(self):
        """Same rule for the game window: clicking back into it must not restart the game."""
        src = WEBXDC_JS.read_text(encoding="utf-8")
        o = src[src.index("    async function open(app, opts){"):]
        o = o[:o.index("\n      _live.delete(key);")]
        self.assertIn("if(prev && !prev.dead)", o)
        self.assertIn("if(!reset){", o)
        self.assertIn("return prev;", o)

    def test_posting_an_app_tags_it_so_it_can_be_found(self):
        """`imeta` is multi-letter and no relay indexes it, so without this hashtag an app posted
        from here can only ever be stumbled upon. Emitted inside imetaTagsFor because there are TEN
        call sites and a hand-copied rule in ten places is this repo's most repeated defect."""
        src = APP_JS.read_text(encoding="utf-8")
        fn = src[src.index("  function imetaTagsFor(content){"):]
        fn = fn[:fn.index("\n  }") + 4]
        self.assertIn("out.push(['t', 'webxdc'])", fn)
        self.assertIn("m application/x-webxdc", fn)

    def test_the_view_is_reachable_from_every_surface(self):
        """A view with no door is the shape Folder Sync shipped in: all the code, no way in."""
        app = APP_JS.read_text(encoding="utf-8")
        self.assertIn('data-view="xdc"', TPL.read_text(encoding="utf-8"))          # the sidebar
        self.assertIn("VIEW==='xdc'", app)                                          # the dispatch
        self.assertIn("xdc:'Webxdc", app)                                           # the title
        self.assertIn("['xdc','gamepad','Webxdc']", app)                            # the phone sheet
        self.assertIn("{ view:'xdc', into:'#games-sub'", app)                       # a stale shell
        os_js = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")
        self.assertIn("'holdem', 'xdc'", os_js)                                     # the desktop folder



class SwCacheKeepList(unittest.TestCase):
    """THE .xdc ARCHIVES MUST SURVIVE A DEPLOY.

    The service worker's activate sweep is a KEEP-LIST — it deletes every cache it does not
    recognise — and `pc-webxdc-v1` was not on it. So every UI deploy that bumped CACHE silently threw
    away every mini app the user had downloaded: 5.8 MB for Quake III, 178 MB for the Half-Life port,
    re-fetched byte-for-byte identical because they are content-addressed. Nothing said so; the app
    just took a minute to open, on mobile data.

    Same shape as the three auto-cleaners Notes had to be exempted from — the sweep is right by
    default and catastrophic for anything nobody told it about.
    """

    def test_the_archive_cache_is_kept(self):
        sw = SW_JS.read_text(encoding="utf-8")
        self.assertIn("WEBXDC_CACHE = 'pc-webxdc-v1'", sw)
        act = sw[sw.index("self.addEventListener('activate'"):]
        act = act[:act.index("});") + 3]
        self.assertIn("k !== WEBXDC_CACHE", act)

    def test_the_name_matches_the_one_webxdc_actually_opens(self):
        """Two spellings would pass the test above and still delete the real cache."""
        sw_name = SW_JS.read_text(encoding="utf-8").split("WEBXDC_CACHE = '")[1].split("'")[0]
        xdc_name = WEBXDC_JS.read_text(encoding="utf-8").split("const CACHE = '")[1].split("'")[0]
        self.assertEqual(sw_name, xdc_name)

    def test_the_two_workers_on_this_origin_do_not_delete_each_others_caches(self):
        """CacheStorage is per-ORIGIN, not per-scope. `static/sw.js` (the main PWA, scope `/`) and
        `static/js/client/sw.js` (the Nostr client, scope `/client/`) share one origin, and each
        swept everything it did not recognise — so they destroyed each other's caches whenever
        either version bumped. That took the client's shell, its media cache, the encrypted drive
        AND the downloaded mini apps with it, and nothing said so; each app just got slow once in a
        while. Namespaced now: `posterchanai-` one side, `pc-` the other."""
        root = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        root_act = root[root.index("self.addEventListener('activate'"):]
        root_act = root_act[:root_act.index("});") + 3]
        self.assertIn("startsWith(CACHE_PREFIX)", root_act)
        self.assertIn("CACHE_PREFIX = 'posterchanai-'", root)

        client = SW_JS.read_text(encoding="utf-8")
        act = client[client.index("self.addEventListener('activate'"):]
        act = act[:act.index("});") + 3]
        self.assertIn("k.startsWith('pc-')", act)
        # …and the two namespaces must not overlap, or the guard above means nothing.
        import re
        for name in re.findall(r"^const \w*CACHE\w* = '([^']+)'", client, re.M):
            self.assertTrue(name.startswith("pc-"), f"{name} is outside the client's namespace")
        self.assertTrue(root.split("CACHE_NAME = '")[1].split("'")[0].startswith("posterchanai-"))

    def test_every_cache_the_sw_names_is_on_the_keep_list(self):
        """The rule, not just this instance of it: a cache added to sw.js without being kept is
        deleted by the next activate, which is how this bug happened in the first place."""
        sw = SW_JS.read_text(encoding="utf-8")
        import re
        names = set(re.findall(r"^const (\w*CACHE) = '", sw, re.M))
        act = sw[sw.index("self.addEventListener('activate'"):]
        act = act[:act.index("});") + 3]
        for n in names:
            if n == "CACHE":
                self.assertIn("k !== CACHE", act)
            else:
                self.assertIn(f"k !== {n}", act, f"{n} is not kept — activate will delete it")


if __name__ == "__main__":
    unittest.main()
