"""A deletion is published on POSITIVE PROOF only — never inferred from a listing.

The design flaw behind four days of trashed files: the engine read "the scan did not see it" as
"the user deleted it", so every way a scan fails to SEE (unmounted drive, revoked grant, flaky
provider) became a deletion published to every device. The executor now probes each claim:
ENOENT under a healthy parent is a deletion; anything else is UNKNOWN and deletes nothing — and a
build whose fs cannot answer confirms nothing at all. The shipped executor is RUN here."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOLDERSYNC = os.path.join(ROOT, "static", "js", "client", "foldersync.js")
EXEC = os.path.join(ROOT, "static", "js", "client", "syncexec.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class DeletionProofTests(unittest.TestCase):
    def _sweep(self, confirm_js):
        """One journaled file that the scan no longer lists → the engine plans a tombstone; what the
        executor ANNOUNCES depends on the proof the fs gives it."""
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const entry = { v:1, by:'me', sha:'s1', csum:'c1', size:9, mtime:10 };
          const fs = {
            scanPage: async (id, so, off) => ({ files: {}, done: true }),   // the file is not listed
            %s
          };
          let published = null;
          const io = {
            index: async () => ({ 'gone.txt': Object.assign({}, entry, { local:{size:9,mtime:10,csum:'c1'} }) }),
            views: async () => ({ views: { me: { 'gone.txt': entry } }, missing: 0 }),
            saveIndex: async () => {},
            publish: async (k, mine) => { published = JSON.parse(JSON.stringify(mine)); },
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me' });
          const tomb = published && published['gone.txt'] && published['gone.txt'].deletedAt;
          process.stdout.write(JSON.stringify({
            announced: !!tomb, removed: rep.removedRemote,
            held: (rep.unconfirmedAbsent || []).map(x => x.why) }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), confirm_js)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_a_proven_absence_is_announced(self):
        out = self._sweep("confirmGone: async () => ({ gone: true, parentAlive: true }),")
        self.assertTrue(out["announced"])
        self.assertEqual(out["removed"], ["gone.txt"])

    def test_an_unreadable_parent_deletes_nothing(self):
        """The unmounted-drive case: the whole folder is unreachable, the listing was empty, and the
        old design published a deletion for every file the journal knew."""
        out = self._sweep("confirmGone: async () => ({ gone: false, parentAlive: false }),")
        self.assertFalse(out["announced"], "an unreachable folder published a deletion")
        self.assertEqual(out["removed"], [])
        self.assertIn("its folder could not be read", out["held"][0])

    def test_a_file_the_probe_still_sees_deletes_nothing(self):
        """The lying-listing case: enumeration omitted the file, the direct probe finds it."""
        out = self._sweep("confirmGone: async () => ({ gone: false, parentAlive: true }),")
        self.assertFalse(out["announced"])
        self.assertIn("still there", out["held"][0])

    def test_a_build_that_cannot_confirm_confirms_nothing(self):
        out = self._sweep("")   # no confirmGone at all — a stale shell
        self.assertFalse(out["announced"], "a stale build without the probe still published deletions")
        self.assertIn("cannot confirm deletions", out["held"][0])


if __name__ == "__main__":
    unittest.main()
