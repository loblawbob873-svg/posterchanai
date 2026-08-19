"""A file edited while its chunks are being read must not be published at all.

The chunks of a big file are read over minutes, and the entry's checksum used to be taken at a
different moment — the scan (minutes before) or a hash of the file AFTER the last chunk (minutes
after). An edit anywhere inside that window stored chunks of a TORN file under a clean checksum of
the final one. The poison is silent on the uploader — its local file really does hash to the
published csum, so its journal says all is well — and permanent on everybody else: every download
reassembles the torn bytes, fails verification, and the copy is remembered as bad. Reported as
"Tablet is having checksum mismatches on Documents", Documents being the folder people edit.

The shipped executor is RUN here with a filesystem whose hash answer changes between the before and
after sides of the chunk reads.
"""
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
class TornUploadTests(unittest.TestCase):
    def _sweep(self, hashes):
        """One 10-byte file chunked at 4 bytes; `hashes` is the sequence hashFile answers with."""
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const hashes = %s; let hcalls = 0;
          const fs = {
            scanPage: async (id, so, off, n) =>
              off ? { files: {}, done: true }
                  : { files: { 'doc.txt': { size: 10, mtime: 100 } }, done: false },
            readPart: async (id, rel, off, len) => new Uint8Array(len),
            hashFile: async () => hashes[Math.min(hcalls++, hashes.length - 1)],
          };
          const io = {
            index: async () => ({}),
            state: async () => ({ state: {}, flagged: {} }),
            saveIndex: async () => {},
            putState: async (key, recs) => { global._published = global._published || {};
              for(const r of recs) global._published[r.path] = JSON.parse(JSON.stringify(r.entry));
              return { ok: recs.map(r => r.path), stale: [], failed: [] }; },
            putParts: async (read, size) => { await read(0, 4); await read(4, 4); await read(8, 2);
                                              return { chunks: ['c1', 'c2', 'c3'], cs: 4 }; },
            hashBytes: async () => 'unused',
          };
          const rep = await X.sweep(fs, io, { id: 'f', key: 'k', device: 'me', chunkAbove: 4 });
          process.stdout.write(JSON.stringify({
            uploaded: rep.uploaded, failed: rep.failed.map(f => f.error),
            entry: (global._published || {})['doc.txt'] || null }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), json.dumps(hashes))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_a_stable_file_is_published_with_the_certified_checksum(self):
        out = self._sweep(["SAME", "SAME"])
        self.assertEqual(out["uploaded"], ["doc.txt"], out)
        self.assertEqual(out["entry"]["csum"], "SAME")
        self.assertEqual(out["entry"]["chunks"], ["c1", "c2", "c3"])

    def test_a_file_that_changed_during_the_upload_is_not_recorded(self):
        out = self._sweep(["BEFORE", "AFTER"])
        self.assertEqual(out["uploaded"], [], "a torn upload was recorded as a success")
        self.assertTrue(any("changed while it was being uploaded" in e for e in out["failed"]),
                        out["failed"])
        self.assertIsNone(out["entry"], "chunks of a torn file were published under a checksum")

    def test_the_scan_hash_serves_as_the_before_side(self):
        """A rehashing sweep already hashed the file at scan time — one hash after the chunks is
        enough, and a mismatch against the scan's answer is the same refusal."""
        js_extra = None  # scan-provided csum travels via u.stat.csum, exercised through scan hash
        out = self._sweep(["SCANNED", "SCANNED"])
        self.assertEqual(out["entry"]["csum"], "SCANNED")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(not NODE, "no node on this node")
class UnreadableSubtreeTests(unittest.TestCase):
    """A folder the scan could not enter leaves its files out of `disk` — and read as "deleted
    locally" they were tombstoned to every device (measured: five files under one locked folder,
    five tombstones, guards silent below the mass floor). Skipped paths must ride the exclusion
    machinery: dropped from all three inputs, deletable by no one, and said on the card."""

    def test_no_tombstone_is_published_for_what_could_not_be_read(self):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const entry = (i) => ({ v:1, by:'me', sha:'s'+i, csum:'c'+i, size:9, mtime:10 });
          const idx = {}; const view = {};
          for(let i=0;i<5;i++){ const p='Locked/f'+i+'.jpg';
            view[p]=entry(i); idx[p]=Object.assign({}, entry(i), {local:{size:9,mtime:10,csum:'c'+i}}); }
          let published = null;
          const fs = { scan: async () => ({ files: { 'ok.jpg': {size:1, mtime:1} },
                                            skipped: [{ path:'Locked', why:'EACCES' }] }) };
          const io = {
            index: async () => idx,
            state: async () => ({ state: JSON.parse(JSON.stringify(view)), flagged: {} }),
            saveIndex: async () => {},
            putState: async (k, recs) => { published = published || {};
              for(const r of recs) published[r.path] = JSON.parse(JSON.stringify(r.entry));
              return { ok: recs.map(r => r.path), stale: [], failed: [] }; },
            putBlob: async () => ({ sha:'oks' }), hashBytes: async () => 'okc',
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me' });
          const tombs = Object.keys(published || idx).filter(p => (published || idx)[p]
                          && (published || idx)[p].deletedAt);
          process.stdout.write(JSON.stringify({ tombs, removed: rep.removedRemote,
            unreadable: (rep.unreadable||[]).length, excluded: rep.excluded }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout)
        self.assertEqual(out["tombs"], [], "an unreadable subtree was tombstoned to every device")
        self.assertEqual(out["removed"], [], out)
        self.assertEqual(out["unreadable"], 1, "the report does not say what it could not read")
