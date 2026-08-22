"""Discover → Git, on the client side — the half nothing tested.

The git HOST has five test files (push auth, browse/edit, serve, proxy, an end-to-end push). The
BROWSER half had none, and that is the half that kept breaking: the last failure was `openRepo`
assigning `S.VIEW`, which app.js exposes with a getter and no setter, so in a strict module the
function threw on its FIRST line and every repo in the list was unopenable. Nothing said so beyond a
generic "something went wrong" toast.

Two kinds of test here, and the split is deliberate:

  * the pure helpers are RUN under node against real kind-30617 events — repo identity, the naddr
    used as a shareable URL, ownership, which host a repo lives on, the clone-URL cleanup;
  * the contract between git.js and app.js is pinned structurally, because that is the shape the
    real failure took: a caller and an export that disagree about whether a property can be written.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = os.path.join(ROOT, "static", "js", "client")
GIT = os.path.join(CLIENT, "git.js")
APP = os.path.join(CLIENT, "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _src(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _lift(names):
    """Take named helpers out of git.js's IIFE so they can be run.

    Lifted rather than reimplemented: a paraphrase of `_repoNaddr` in a test proves only that the
    paraphrase works, and this file exists because the shipped code was wrong.
    """
    src = _src(GIT)
    out = []
    for n in names:
        m = re.search(r"\n  (?:async )?function %s\(" % re.escape(n), src)
        assert m, "%s moved in git.js — re-point this test" % n
        start = m.start() + 1
        i = src.index("{", m.end() - 1)
        depth = 0
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(src[start:i + 1])
    return "\n".join(out)


@unittest.skipIf(not NODE, "no node on this node")
class RepoIdentityTests(unittest.TestCase):
    """A repo is a kind-30617; everything the list draws comes off its tags."""

    def _run(self, body, extra=""):
        # The two things the lifted helpers reach for: the shared surface (`S.ME` decides ownership,
        # including via a `maintainers` tag) and the profile cache the search haystack folds in.
        js = """
        const S = { ME: { pubkey: 'a'.repeat(64) } };
        const profOf = () => ({});
        %s
        %s
        """ % (_lift(["_repoTag", "_repoAddr", "_repoIsMine", "_repoHostname", "_repoHaystack"])
               + "\n" + extra, body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout or "null")

    def _ev(self, **kw):
        tags = [["d", kw.get("d", "myrepo")]]
        if kw.get("name"):
            tags.append(["name", kw["name"]])
        for c in kw.get("clone", []):
            tags.append(["clone", c])
        for w in kw.get("web", []):
            tags.append(["web", w])
        return {"kind": 30617, "pubkey": kw.get("pubkey", "a" * 64), "tags": tags,
                "content": kw.get("content", "")}

    def test_the_address_is_the_coordinate_the_network_uses(self):
        """`30617:<pubkey>:<d>` — everything that refers to a repo (issues, patches, state) keys on
        it, so a change here silently detaches a repo from all of its own events."""
        ev = self._ev(d="admintools")
        got = self._run("process.stdout.write(JSON.stringify(_repoAddr(%s)))" % json.dumps(ev))
        self.assertEqual(got, "30617:" + "a" * 64 + ":admintools")

    def test_a_repo_with_no_name_still_has_an_identity(self):
        ev = self._ev(d="admintools")
        got = self._run("process.stdout.write(JSON.stringify("
                        "[_repoTag(%s,'name'), _repoTag(%s,'d')]))" % (json.dumps(ev), json.dumps(ev)))
        self.assertEqual(got, ["", "admintools"])

    def test_mine_is_decided_by_the_key_not_by_the_name(self):
        mine = self._ev(pubkey="a" * 64)
        theirs = self._ev(pubkey="b" * 64)
        got = self._run("process.stdout.write(JSON.stringify("
                        "[_repoIsMine(%s), _repoIsMine(%s)]))" % (json.dumps(mine), json.dumps(theirs)))
        self.assertEqual(got, [True, False])

    def test_the_host_comes_from_the_clone_url(self):
        ev = self._ev(clone=["https://poster.place/git/npub1abc/admintools.git"])
        got = self._run("process.stdout.write(JSON.stringify(_repoHostname(%s)))" % json.dumps(ev))
        self.assertIn("poster.place", got or "")

    def test_a_repo_with_no_clone_url_does_not_throw(self):
        """A malformed or half-published repo must not take the whole list down with it — the list is
        drawn from whatever the relay returns, which is not something this client controls."""
        ev = self._ev()
        got = self._run("process.stdout.write(JSON.stringify(_repoHostname(%s) || ''))" % json.dumps(ev))
        self.assertEqual(got, "")

    def test_search_matches_a_repo_by_name_and_by_identifier(self):
        ev = self._ev(d="admintools", name="Admin Tools")
        hay = self._run("process.stdout.write(JSON.stringify(_repoHaystack(%s)))" % json.dumps(ev))
        self.assertIn("admintools", hay.lower())
        self.assertIn("admin tools", hay.lower())


class TheContractWithAppJsTests(unittest.TestCase):
    """The shape the last failure took: a caller and an export that disagree.

    `openRepo` assigns `S.VIEW`. app.js exposed `VIEW` with a getter only, so the assignment threw —
    in a strict module that is a TypeError, and it killed the function on its first line. Every repo
    was unopenable and the only symptom was the generic error toast.
    """

    @classmethod
    def setUpClass(cls):
        cls.git = _src(GIT)
        cls.app = _src(APP)

    def test_open_repo_can_set_the_view(self):
        """RUN, never grep: a `set VIEW` existed on window.__PC for a whole evening while git.js
        bound `const S = dep.state` — a DIFFERENT object, still getter-only — and the grep version
        of this test stayed green while every repo stayed unopenable. The shipped `state:` facade is
        extracted and the assignment git.js performs is performed on it, in strict mode."""
        self.assertIn("S.VIEW='repo'", self.git.replace(" ", ""))
        self.assertIn("const S = dep.state", self.git,
                      "git.js re-bound its surface — repoint this test at whatever it binds now")
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("no node on this node")
        js = """
        const src = require('fs').readFileSync(%s,'utf8');
        const at = src.indexOf('state: {');
        if(at < 0){ console.error('no state facade'); process.exit(1); }
        const open = src.indexOf('{', at);
        let d=0, i=open;
        while(i < src.length){ if(src[i]==='{')d++; else if(src[i]==='}'){d--; if(!d)break;} i++; }
        const lit = src.slice(open, i+1);
        const got = new Function("'use strict'; let VIEW='home', CFG={}, ME=null, GUEST=false, " +
          "LOGO=''; const state = " + lit + "; state.VIEW='repo'; return state.VIEW;")();
        if(got !== 'repo'){ console.error('facade rejected the write: ' + got); process.exit(1); }
        console.log('ok');
        """ % __import__("json").dumps(APP)
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "openRepo's S.VIEW= throws on the object git.js actually receives:\n"
                         + r.stderr[-1500:])

    def test_every_helper_git_js_borrows_from_app_js_exists(self):
        """git.js reaches into the shared surface for a dozen things. One rename there and the repo
        view dies at runtime with nothing to point at."""
        used = set(re.findall(r"\bS\.([a-zA-Z_][A-Za-z0-9_]*)\b", self.git))
        # Anything the export object defines, in any of the forms it uses.
        for name in sorted(used):
            defined = (
                re.search(r"[,{]\s*%s\s*[,:(]" % re.escape(name), self.app)
                or re.search(r"\b(?:get|set)\s+%s\s*\(" % re.escape(name), self.app)
                or re.search(r"\b%s\s*[:=]" % re.escape(name), self.app)
            )
            self.assertTrue(defined, "git.js uses S.%s and app.js does not define it" % name)

    def test_the_repo_view_is_not_a_nav_destination(self):
        """It sets VIEW directly and does its own nav clearing precisely because `repo` is not in the
        sidebar — routing it through switchView would fight the line after it."""
        at = self.git.index("function openRepo(")
        body = self.git[at:at + 800]
        self.assertIn("_clearNav()", body)
        self.assertNotIn("switchView('repo')", body)

    def test_bug_reports_use_the_shared_mention_picker_and_publish_p_tags(self):
        """Typing @name in an issue must behave like the normal composer, and choosing somebody
        must notify them. The picker without mentionTags would only make pretty text; mentionTags
        without the picker is the reported missing dropdown."""
        at = self.git.index("function newRepoIssue(")
        body = self.git[at:self.git.index("\n  // ----------", at)]
        self.assertIn("attachMentionAutocomplete(ta)", body)
        self.assertIn("mentionTags(body).forEach", body)
        self.assertIn("!tags.some(x=>x[0]==='p'&&x[1]===t[1])", body,
                      "a maintainer who is also mentioned would get duplicate p tags")

        # The factory boundary is explicit: both names must cross it or the issue modal throws only
        # when opened, despite both helpers existing in app.js.
        deps = self.app[self.app.index("window.PCGitFactory({"):
                        self.app.index("}));", self.app.index("window.PCGitFactory({"))]
        self.assertIn("attachMentionAutocomplete", deps)
        self.assertIn("mentionTags", deps)


class GitJsParsesTests(unittest.TestCase):
    @unittest.skipIf(not NODE, "no node on this node")
    def test_it_parses(self):
        r = subprocess.run([NODE, "--check", GIT], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])


class StarTests(unittest.TestCase):
    """⭐ repos — a NIP-51 bookmark set (30003, d:'git-repos') of 30617 coordinates, filterable as
    Mine / ⭐ Starred / All. The replaceable-list rules are load-bearing and pinned: no write after a
    failed read (a null star set keeps the buttons read-only), writes serialized, and a refused
    publish rolls the optimistic toggle back."""

    @classmethod
    def setUpClass(cls):
        cls.git = _src(GIT)

    def test_the_list_is_nip51_and_carries_repo_coordinates(self):
        at = self.git.index("STARS_D")
        seg = self.git[at:at + 2400]
        self.assertIn("30003", seg)
        self.assertIn("30617:", seg)
        self.assertIn("'#d':[STARS_D", seg.replace('"', "'"))
        self.assertIn("pick(30003, STARS_D)", seg,
                      "our own set is no longer selected by its d tag")

    def test_a_failed_read_keeps_the_buttons_read_only(self):
        at = self.git.index("async function _loadStars()")
        seg = self.git[at:at + 3400]
        self.assertIn("catch(_){", seg)
        self.assertNotIn("_stars=new Set()", seg.split("catch")[1],
                         "a failed read minted an empty list — the follows-wipe, for stars")
        at2 = self.git.index("function toggleStar(")
        self.assertIn("_stars===null", self.git[at2:at2 + 600],
                      "toggling with no successful read would publish an empty list")

    def test_a_refused_publish_rolls_the_star_back(self):
        at = self.git.index("function toggleStar(")
        seg = self.git[at:at + 2600]
        self.assertIn("r.ok===false", seg)
        self.assertIn("if(on){ _starsMine.delete(addr); _starsGw.delete(addr); }", seg)
        self.assertIn("_stars=new Set([..._starsMine, ..._starsBk]);", seg,
                      "the rollback fixes our set but leaves the union stale on screen")

    def test_gitworkshop_bookmarks_count_as_stars_and_are_never_written(self):
        """Measured on the live relay: gitworkshop bookmarks repos in the STANDARD kind-10003 list.
        Starred is the union of that and our own 30003 set — and writes only ever touch ours,
        because a write into somebody's 10003 is one failed read away from wiping the rest of
        their bookmarks."""
        at = self.git.index("async function _loadStars()")
        seg = self.git[at:at + 3400]
        self.assertIn("10003", seg)
        self.assertIn("_starsBk", seg)
        at2 = self.git.index("function toggleStar(")
        tseg = self.git[at2:at2 + 2000]
        self.assertIn("_starsMine", tseg)
        self.assertNotIn("publish(10003", tseg, "we wrote into the user's general bookmarks list")
        self.assertIn("remove it there", tseg,
                      "unstarring a foreign bookmark silently fails instead of saying whose it is")

    def test_gitworkshops_set_is_written_with_read_modify_write_discipline(self):
        """gitworkshop's set (30003 d:'git-repo-bookmark') is read AND written — a star made here
        must show on the ngit site, whose own ⭐ publishes nothing unless that tab can sign. The
        write is read-modify-write: every non-a tag of the newest version is carried, only the
        coordinates are replaced, and a failed mirror never rolls back our own set. The general
        10003 bookmarks list stays unwritten — it belongs to other features."""
        at = self.git.index("async function _loadStars()")
        seg = self.git[at:at + 2600]
        self.assertIn("_gwCur=pick(30003, 'git-repo-bookmark')", seg,
                      "the raw newest version is not kept — the mirror would drop foreign tags")
        at2 = self.git.index("function toggleStar(")
        tseg = self.git[at2:at2 + 3000]
        self.assertIn("['d','git-repo-bookmark']", tseg.replace('"', "'"))
        self.assertIn("t[0]!=='a'&&t[0]!=='d'", tseg, "foreign tags are not carried through")
        self.assertIn("_starsGw", tseg)
        self.assertNotIn("publish(10003", tseg, "the general bookmarks list was written")

    def test_gitworkshop_stars_are_reactions_and_they_count(self):
        """MEASURED against the live site with an instrumented window.nostr (2026-08-18): the ngit
        Star button signs a KIND-7 REACTION a-tagging the 30617 — no list anywhere. Three of the
        user's "lost" stars sat in the store as '+' reactions while every list-shaped read came back
        empty. So: reactions join the read (newest per repo wins, '-' is an unstar), and OUR unstar
        publishes the NIP-09 delete gitworkshop itself honours."""
        at = self.git.index("async function _loadStars()")
        seg = self.git[at:at + 3400]
        self.assertIn("kinds:[7], authors:[S.ME.pubkey]", seg.replace('"', "'"),
                      "the user's reactions are never read — ngit stars stay invisible")
        self.assertIn("_starsRx", seg)
        self.assertIn("(ev.content||'+') !== '-'", seg, "a '-' reaction counts as a star")
        at2 = self.git.index("function toggleStar(")
        tseg = self.git[at2:at2 + 3000]
        self.assertIn("publish(5, ", tseg, "unstarring a reaction-star deletes nothing")
        self.assertIn("['e', rx.id]", tseg.replace('"', "'"))

    def test_stars_refresh_on_every_entry_not_once_per_page(self):
        """"i starred a repo on ngit again and still does not appear" — the star was on the relay
        and the view kept answering from the set it loaded at first open. The first load may block;
        every later entry must refresh behind the cached paint and repaint on arrival."""
        at = self.git.index("async function renderRepos()")
        seg = self.git[at:at + 2200]
        self.assertIn("if(_stars===null) await _loadStars();", seg)
        self.assertIn("else _loadStars().then(", seg,
                      "a page that loaded stars once never sees a star made elsewhere")
        after = seg.split("else _loadStars().then(")[1][:120]
        self.assertIn("paint()", after, "the refresh arrives and nothing redraws")

    def test_the_relay_syncs_the_bookmark_kinds_in(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        src = open(os.path.join(root, "app", "services", "nostr_relay", "thread.py"),
                   encoding="utf-8").read()
        m = re.search(r'nostr_relay_ingest_kinds", "([0-9,]+)"', src)
        kinds = m.group(1).split(",")
        for k in ("10003", "30003"):
            self.assertIn(k, kinds,
                          "kind %s never syncs in — stars made in other clients cannot arrive" % k)

    def test_the_scope_row_offers_starred(self):
        self.assertIn('data-scope="starred"', self.git)
        self.assertIn("repos.filter(_starred)", self.git)

    def test_an_empty_star_list_never_strands_the_view(self):
        self.assertIn("_repoScope==='starred' && (!_stars || !_stars.size)", self.git,
                      "landing on an empty Starred scope shows nothing with no way out")


@unittest.skipIf(not NODE, "no node on this node")
class WhichReposThisNodeCanBrowseTests(unittest.TestCase):
    """The repo view's Files / Commits / branch switcher, and who they may be offered to.

    Reported as "clicking on a git repo gets stuck and never loads" in the desktop app. The decision
    was made on the URL SHAPE alone — `…/<npub>/<repo>.git` — and every GRASP forge on nostr uses
    that shape. Measured against the live node: for a repo hosted on relay.ngit.dev, our own
    /client/git/refs and /tree answer 404 in ~20ms and /readme spends 8-9 SECONDS timing out
    against a host that is not a forge. Nine seconds of spinner, then a page whose every panel says
    it could not read anything. The majority of what Discover → Git lists is exactly those repos.

    Two facts have to hold together, which is why they are tested together: the repo must be hosted
    HERE, and "here" must be the INSTANCE — in the desktop app and the APK the page origin is the
    bundle (`app://posterchan`), so a check against `location.origin` answers no to the node's own
    repos and yes to nothing at all.
    """

    OURS = "https://poster.place/git/npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr/posterchanai.git"
    # Real clone URLs, taken off relay.poster.place — these are what the list is actually full of.
    FOREIGN = [
        "https://relay.ngit.dev/npub107jk7htfv243u0x5ynn43scq9wrxtaasmrwwa8lfu2ydwag6cx2quqncxg/grimoire.git",
        "https://git.gittr.space/npub1n2ph08n4pqz4d3jk6n2p35p2f4ldhc5g5tu7dhftfpueajf4rpxqfjhzmc/gittr.git",
        "https://pyramid.fiatjaf.com/npub1ye5ptcxfyyxl5vjvdjar2ua3f0hynkjzpx552mu5snj3qmx5pzjscpknpr/fips.git",
    ]

    def _ask(self, clone, *, host_base="", create=True, page_origin="https://poster.place",
             server_origin="https://poster.place"):
        """Run the SHIPPED helpers against one clone URL, with the surface they really read."""
        js = """
        const S = { CFG: { git_host_base: %s, git_create_available: %s } };
        const self = { location: { origin: %s } };
        const _serverOrigin = () => %s;
        %s
        const ev = { kind:30617, pubkey:'a'.repeat(64), tags:[['d','r'],['clone', %s]] };
        process.stdout.write(JSON.stringify(
          { shaped: _graspShaped(%s), here: _repoHostedHere(ev), browsable: _repoBrowsableHere(ev),
            base: _gitHostBase() }));
        """ % (json.dumps(host_base), "true" if create else "false", json.dumps(page_origin),
               json.dumps(server_origin),
               _lift(["_gitHostBase", "_repoHostname", "_repoHostedHere", "_graspShaped",
                      "_repoBrowsableHere"]),
               json.dumps(clone), json.dumps(clone))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout or "null")

    def test_our_own_repo_is_browsable(self):
        got = self._ask(self.OURS)
        self.assertTrue(got["shaped"])
        self.assertTrue(got["browsable"], "the node can no longer browse its own repos: %r" % got)

    def test_a_repo_on_another_grasp_forge_is_not_browsable_here(self):
        """This is the regression. Each of these has the right SHAPE and lives somewhere else, and
        asking our git host about it can only 404 — after eight seconds of README timeout."""
        for clone in self.FOREIGN:
            got = self._ask(clone)
            self.assertTrue(got["shaped"], "%s no longer parses as a GRASP url" % clone)
            self.assertFalse(got["browsable"],
                             "%s is hosted elsewhere but the repo view would offer Files, Commits "
                             "and a branch switcher for it, all of which can only fail: %r"
                             % (clone, got))

    def test_the_host_is_the_instance_not_the_page(self):
        """The desktop app and the APK serve the client from their own bundle, so `location.origin`
        is `app://posterchan`. Reading the host from there answered no to the node's OWN repos (no
        Files tab in the app at all) and made createRepo mint `app://posterchan/git/<npub>/<id>.git`
        as a clone URL — and sign a NIP-98 token for it."""
        got = self._ask(self.OURS, page_origin="app://posterchan",
                        server_origin="https://poster.place")
        self.assertEqual(got["base"], "https://poster.place/git",
                         "the git host base is being read off the page origin, not the instance")
        self.assertTrue(got["browsable"],
                        "the bundled app cannot browse the instance's own repos: %r" % got)

    def test_with_no_instance_nothing_is_hosted_here(self):
        """Standalone (relays only). There is no node to ask, so no repo may claim to be browsable —
        the alternative is a Files tab whose every fetch is rejected before it leaves the app."""
        got = self._ask(self.OURS, page_origin="app://posterchan", server_origin="")
        self.assertEqual(got["base"], "")
        self.assertFalse(got["browsable"])

    def test_a_node_with_no_git_host_claims_nothing(self):
        got = self._ask(self.OURS, host_base="", create=False)
        self.assertEqual(got["base"], "")
        self.assertFalse(got["here"])

    def test_an_explicit_git_host_base_wins(self):
        got = self._ask("https://git.example.org/npub1abc/thing.git",
                        host_base="https://git.example.org/", server_origin="https://poster.place")
        self.assertEqual(got["base"], "https://git.example.org")
        self.assertTrue(got["browsable"])

    def test_the_rule_this_replaced_would_have_said_yes(self):
        """Proof the check above can fail — the pre-fix rule, re-run over the same fixtures.

        `isGrasp` was this expression inline in openRepo, and nothing else. It asks only about the
        shape, so it answers YES for every GRASP forge on the network. Without this, the tests above
        pass on any code that happens to define the helper, and say nothing about what it decides.
        """
        js = """
        const shapeOnly = (cloneUrl) => { try{
          const sg=new URL(cloneUrl).pathname.split('/').filter(Boolean);
          const gi=sg.findIndex(s=>s.endsWith('.git'));
          return gi>0 && (/^npub1/.test(sg[gi-1])||/^[0-9a-fA-F]{64}$/.test(sg[gi-1]));
        }catch(_){ return false; } };
        process.stdout.write(JSON.stringify(%s.map(shapeOnly)));
        """ % json.dumps(self.FOREIGN)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(json.loads(r.stdout), [True] * len(self.FOREIGN),
                         "the fixtures no longer exercise the bug — pick real foreign GRASP clone "
                         "URLs that the shape-only rule accepts")
