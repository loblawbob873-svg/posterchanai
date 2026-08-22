"""The chunked-download resume, RUN — the shipped `getParts` out of app.js, not a copy of it.

A 2 GB .jex restarted from zero on every attempt, on two devices, repeatedly. The cause was one
condition in the resume arithmetic: `have % cs === 0`, which demanded that the interruption happened
exactly on a chunk boundary. Almost nothing does — a stall, a dropped radio, a killed renderer or a
closed lid all stop part-way through a chunk — so `skip` stayed 0 and every attempt threw away every
byte it had.

On a small file that is invisible: it finishes inside one attempt either way. On a large one it is
fatal, and the size is what decides it, because the bigger the file the likelier it is to be
interrupted before it can finish. That is the shape of a bug that only ever appears on the files
least able to afford it.

This extracts the function from the shipped bundle and drives it, because the sims all reimplement
the chunker — the real one had no coverage at all.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def extract(src, header):
    """The function as shipped, by brace matching — so the test cannot drift from the code."""
    i = src.index(header)
    depth, j = 0, src.index("{", i)
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError("unbalanced braces")


@unittest.skipIf(not NODE, "no node on this node")
class ChunkedResume(unittest.TestCase):
    def setUp(self):
        with open(APP, encoding="utf-8") as fh:
            self.src = fh.read()
        self.fn = extract(self.src, "async getParts(chunks, writePart, expect, have, cs)")

    def run_js(self, chunks, expect, have, cs):
        js = """
        const impl = { %s };
        (async () => {
          const writes = []; let bumps = 0;
          const chunks = %s;
          const n = await impl.getParts(chunks,
            async (off, bytes) => { writes.push([off, bytes.length]); },
            %s, %s, %s, () => { bumps++; });
          process.stdout.write(JSON.stringify({ rebuilt: n, writes, bumps }));
        })().catch(e => { process.stdout.write(JSON.stringify({ threw: String(e.message || e) })); });
        """ % (self.fn.replace("_syncBlobBytes(sha, onWireProgress)", "FAKE(sha, onWireProgress)"),
               json.dumps(chunks), json.dumps(expect), json.dumps(have), json.dumps(cs))
        # Every chunk is `cs` bytes except the last, which is short — the real shape.
        js = ("const CS = %s, EXPECT = %s;\n"
              "const FAKE = async (sha, progress) => { const i = Number(String(sha).split('-')[1]);\n"
              "  progress(1, 2); progress(2, 2);\n"
              "  const last = Math.ceil(EXPECT / CS) - 1;\n"
              "  return new Uint8Array(i === last ? (EXPECT - last * CS) : CS); };\n" % (cs, expect)) + js
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w") as fh:
                fh.write(js)
            r = subprocess.run([NODE, p], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-900:])
        return json.loads(r.stdout)

    def test_a_transfer_stopped_mid_chunk_resumes_at_the_last_whole_chunk(self):
        """The reported failure. 2 GB, 4 MB chunks, interrupted 1.5 chunks in."""
        cs, total = 4 * 1024 * 1024, 512
        expect = cs * (total - 1) + 1000
        out = self.run_js([f"c-{i}" for i in range(total)], expect, cs + 1024, cs)
        self.assertNotIn("threw", out, out)
        first = out["writes"][0][0]
        self.assertEqual(first, cs,
                         "the download restarted at byte 0 after stopping mid-chunk — on a file this "
                         "size that is every byte, every attempt, for ever")
        self.assertEqual(len(out["writes"]), total - 1, "it re-fetched chunks it already had")

    def test_a_clean_boundary_still_resumes(self):
        cs, total = 4 * 1024 * 1024, 8
        expect = cs * (total - 1) + 500
        out = self.run_js([f"c-{i}" for i in range(total)], expect, cs * 3, cs)
        self.assertEqual(out["writes"][0][0], cs * 3)

    def test_the_rebuilt_size_is_still_checked(self):
        """The size check is what stops a short rebuild being committed as a whole file."""
        cs, total = 1024, 4
        expect = cs * 3 + 10
        out = self.run_js([f"c-{i}" for i in range(total)], expect, cs + 5, cs)
        self.assertNotIn("threw", out, out)
        self.assertEqual(out["rebuilt"], expect)

    def test_nothing_on_disk_starts_at_zero(self):
        cs, total = 1024, 4
        out = self.run_js([f"c-{i}" for i in range(total)], cs * 3 + 10, 0, cs)
        self.assertEqual(out["writes"][0][0], 0)

    def test_a_part_file_longer_than_the_real_file_is_not_trusted(self):
        """A stale part left by an EARLIER version of the path can be longer than this one. The last
        chunk is short, so an offset past `expect` fails the size check on every attempt — and the
        throw happens inside here, so the caller's discard never runs and the path can never be
        downloaded again. It falls back to a full download instead."""
        cs, total = 1024, 4
        expect = cs * 3 + 10
        out = self.run_js([f"c-{i}" for i in range(total)], expect, cs * 9, cs)
        self.assertNotIn("threw", out, out)
        self.assertEqual(out["writes"][0][0], 0, "a stale over-long part file was resumed from")

    def test_a_part_file_shorter_than_one_chunk_starts_over(self):
        """There is no whole chunk to keep, and half a chunk proves nothing."""
        cs, total = 4096, 4
        out = self.run_js([f"c-{i}" for i in range(total)], cs * 3 + 10, 100, cs)
        self.assertEqual(out["writes"][0][0], 0)

    def test_wire_reads_reach_the_stall_watchdog(self):
        cs, total = 4096, 4
        out = self.run_js([f"c-{i}" for i in range(total)], cs * 3 + 10, 0, cs)
        self.assertEqual(out["bumps"], total * 2,
                         "byte-level progress was not forwarded; a slow 16 MB chunk will look stalled")


if __name__ == "__main__":
    unittest.main()
