"""An entry published without an address heals itself from whoever holds the file.

"cHlE…mp4 — the shared record does not say where this file is stored" sat on a fresh pair's card
while the desktop, holding the file, settled it as unchanged sweep after sweep — its journal
honestly said applied. The rule (both engines, parity-tested): a live record naming no sha and no
chunks, on a device with a local copy, is a SEND; a device without one keeps reporting, which is
all it can do. The shipped JS engine is RUN here."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = os.path.join(ROOT, "static", "js", "client", "syncstate.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class UnstoredEntryTests(unittest.TestCase):
    def _run(self, body):
        js = "const E = require(%s);\n%s" % (json.dumps(ENGINE), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1200:])
        return json.loads(r.stdout)

    ENTRY = "{ v: 2, by: 'desktop', csum: 'c1', size: 9, mtime: 10 }"   # no sha, no chunks

    def test_the_holder_republishes(self):
        out = self._run("""
          const entry = %s;
          const p = E.plan({ device:'desktop',
            disk: { 'clip.mp4': { size:9, mtime:10, csum:'c1' } },
            index: { 'clip.mp4': Object.assign({}, entry, { local:{ size:9, mtime:10, csum:'c1' } }) },
            state: { 'clip.mp4': entry } });
          process.stdout.write(JSON.stringify({
            send: p.send.map(x=>x.path), trash: p.trash.length, why: (p.send[0]||{}).why||'' }));
        """ % self.ENTRY)
        self.assertEqual(out["send"], ["clip.mp4"],
                         "the holder settles an address-less record as unchanged for ever")
        self.assertEqual(out["trash"], 0)
        self.assertIn("names no storage", out["why"])

    def test_a_device_without_the_file_does_not_pretend_to(self):
        out = self._run("""
          const entry = %s;
          const p = E.plan({ device:'laptop', disk:{}, index:{},
            state: { 'clip.mp4': entry } });
          process.stdout.write(JSON.stringify({ send: p.send.length, trash: p.trash.length }));
        """ % self.ENTRY)
        self.assertEqual(out["send"], 0)
        self.assertEqual(out["trash"], 0, "an address-less record became a delete order")

    def test_a_stored_entry_is_untouched_by_the_rule(self):
        out = self._run("""
          const entry = Object.assign(%s, { sha: 'ab'.repeat(32) });
          const p = E.plan({ device:'desktop',
            disk: { 'clip.mp4': { size:9, mtime:10, csum:'c1' } },
            index: { 'clip.mp4': Object.assign({}, entry, { local:{ size:9, mtime:10, csum:'c1' } }) },
            state: { 'clip.mp4': entry } });
          process.stdout.write(JSON.stringify({ send: p.send.length, unchanged: p.unchanged }));
        """ % self.ENTRY)
        self.assertEqual(out["send"], 0, "a healthy entry is re-published on every sweep")


if __name__ == "__main__":
    unittest.main()
