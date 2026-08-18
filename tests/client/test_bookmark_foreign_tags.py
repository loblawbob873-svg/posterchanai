"""A post bookmark must not erase a repo star another app wrote into the same list.

Kind 10003 is a STANDARD NIP-51 list: our client keeps post bookmarks (e-tags) in it, gitworkshop
keeps repo stars (a-tags) in it. _editEList carried non-e tags from the single newest version its
read returned — and a read that merges a stale cache with the relay returns several versions, so
publishing from one of them erased the other app's tags permanently (measured: the owner's newest
10003 was ours, client-tagged, with the gitworkshop star gone). The shipped _editEList is RUN here
against a two-version read."""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class ForeignTagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = open(APP, encoding="utf-8").read()
        a = src.index("async function _editEList(")
        i = src.index("{", a)
        d = 0
        while i < len(src):
            if src[i] == "{": d += 1
            elif src[i] == "}":
                d -= 1
                if not d: break
            i += 1
        cls.fn = src[a:i + 1]

    def _run(self, versions, add_id="newpost", inmem=None):
        js = """
        const ME = { pubkey: 'me' };
        const PINNED = new Set(), BOOKMARKS = new Set(%s);
        const Relay = { query: async () => %s };
        let published = null;
        const publish = async (kind, content, tags) => { published = { kind, content, tags }; return { ok: true }; };
        %s
        (async () => {
          await _editEList(10003, %s, true);
          process.stdout.write(JSON.stringify(published));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(inmem or []), json.dumps(versions), self.fn, json.dumps(add_id))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    STAR = ["a", "30617:abc:posterchanai"]

    def test_a_stale_newest_version_does_not_erase_the_star(self):
        """The exact wipe: OUR newer version (no star) + the older gitworkshop one (with it)."""
        out = self._run([
            {"created_at": 200, "content": "", "tags": [["client", "PosterChan AI"], ["e", "p1"]]},
            {"created_at": 100, "content": "", "tags": [self.STAR, ["e", "p1"]]},
        ])
        self.assertIn(self.STAR, out["tags"], "the foreign star was erased by a post bookmark")
        ids = [t[1] for t in out["tags"] if t[0] == "e"]
        self.assertIn("p1", ids)
        self.assertIn("newpost", ids)

    def test_the_star_survives_when_it_is_in_the_newest_version(self):
        out = self._run([{"created_at": 200, "content": "", "tags": [self.STAR]}])
        self.assertIn(self.STAR, out["tags"])

    def test_no_duplicate_client_tags(self):
        out = self._run([
            {"created_at": 200, "content": "", "tags": [["client", "PosterChan AI"]]},
            {"created_at": 100, "content": "", "tags": [["client", "gitworkshop"]]},
        ])
        self.assertEqual(len([t for t in out["tags"] if t[0] == "client"]), 0,
                         "carried client tags — publish() stamps its own, this would duplicate")

    def test_our_own_e_edits_still_apply(self):
        out = self._run([{"created_at": 200, "content": "", "tags": [["e", "old"]]}],
                        add_id="fresh", inmem=["held"])
        ids = sorted(t[1] for t in out["tags"] if t[0] == "e")
        self.assertEqual(ids, ["fresh", "held", "old"])


if __name__ == "__main__":
    unittest.main()
