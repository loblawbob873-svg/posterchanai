"""A checksum-bad store copy heals from the device holding the good file.

"the copy in the store fails its checksum — the device that has this file must send it again",
on every receiving device, while the holder settles clean: the torn-era poison. The puller's
refused identities ride its published view (`doc.bad`); the holder verifies its local file
against its journal and re-sends, which mints a new identity and expires every refusal. The
shipped executor is RUN with a sibling view carrying the bad id."""
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
class BadCopyHealTests(unittest.TestCase):
    def _sweep(self, local_hash, bad_ids):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const entry = { v:1, by:'me', sha:'oldsha', csum:'GOODCSUM', size:5, mtime:10 };
          const fs = {
            scanPage: async () => ({ files: { 'r.jpg': { size:5, mtime:10 } }, done: true }),
            hashFile: async () => %s,
            read: async () => new Uint8Array(5),
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          let published = null;
          const badIds = %s;
          const io = {
            index: async () => ({ 'r.jpg': Object.assign({}, entry, { local:{size:5,mtime:10,csum:'GOODCSUM'} }) }),
            state: async () => ({ state: { 'r.jpg': JSON.parse(JSON.stringify(entry)) },
                                  flagged: badIds.length ? { 'r.jpg': badIds[0] } : {} }),
            saveIndex: async () => {},
            putState: async (k, recs) => { published = published || {};
              for(const r of recs) published[r.path] = JSON.parse(JSON.stringify(r.entry));
              return { ok: recs.map(r => r.path), stale: [], failed: [] }; },
            putBlob: async () => ({ sha: 'newsha' }),
            hashBytes: async () => 'GOODCSUM',
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me' });
          process.stdout.write(JSON.stringify({
            uploaded: rep.uploaded, reseeding: rep.reseeding || [], badHere: rep.badHere || [],
            newSha: published && published['r.jpg'] && published['r.jpg'].sha }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), json.dumps(local_hash), json.dumps(bad_ids))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_the_holder_reseeds_a_copy_others_refuse(self):
        out = self._sweep("GOODCSUM", ["oldsha"])
        self.assertEqual(out["reseeding"], ["r.jpg"])
        self.assertIn("r.jpg", out["uploaded"])
        self.assertEqual(out["newSha"], "newsha", "the re-seed did not mint a new identity")

    def test_a_holder_whose_own_copy_is_bad_does_not_reseed_poison(self):
        out = self._sweep("TORN_DIFFERENT", ["oldsha"])
        self.assertEqual(out["reseeding"], [])
        self.assertEqual(out["badHere"], ["r.jpg"], "a locally-bad copy was not reported")
        self.assertNotIn("r.jpg", out["uploaded"])

    def test_no_signal_no_extra_uploads(self):
        out = self._sweep("GOODCSUM", [])
        self.assertEqual(out["reseeding"], [])
        self.assertNotIn("r.jpg", out["uploaded"])


if __name__ == "__main__":
    unittest.main()
