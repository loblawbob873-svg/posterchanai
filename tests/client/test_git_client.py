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
        seg = self.git[at:at + 1700]
        self.assertIn("catch(_){", seg)
        self.assertNotIn("_stars=new Set()", seg.split("catch")[1],
                         "a failed read minted an empty list — the follows-wipe, for stars")
        at2 = self.git.index("function toggleStar(")
        self.assertIn("_stars===null", self.git[at2:at2 + 600],
                      "toggling with no successful read would publish an empty list")

    def test_a_refused_publish_rolls_the_star_back(self):
        at = self.git.index("function toggleStar(")
        seg = self.git[at:at + 1400]
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
        seg = self.git[at:at + 1400]
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
