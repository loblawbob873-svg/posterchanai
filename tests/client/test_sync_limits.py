"""How big a file folder sync will carry, and why.

Run: venv-unified/bin/python -m unittest tests.client.test_sync_limits

The ceiling was 256 MB, set when a file, its ciphertext and the upload body were all in memory at
once — the shape that killed a renderer on a large Pictures folder. Chunked I/O (readPart/writePart)
made that untrue: memory is bounded by ONE CHUNK by construction, whatever the file weighs.

So the number was re-derived by measurement rather than by argument. scripts/measure_chunked_upload.py
pushes real 4 MB chunks at the real Blossom endpoint with real BUD-01 auth: a flat 76 ms each (mean
over 384 MB, p95 80 ms, max 118) at ~50 MB/s, with no drift as the blob count grows. A 5 GB file is
~1280 chunks, about a minute and a half of uploading on a LAN.

The measurement also turned up a sizing bug, which is what this file mostly guards: the SERVER's
limit is per UPLOAD, and with chunking an upload is a CHUNK. Taking the lower of it and the file
ceiling capped synced FILES at the server's per-request maximum while every request actually being
sent was 4 MB — so a node configured with a 100 MB limit silently refused a 200 MB file it could
have carried in fifty pieces.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


def src():
    with open(SYNC, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class TestTheCeilings(unittest.TestCase):
    def test_the_chunked_ceiling_is_gigabytes(self):
        m = re.search(r"const SYNC_MAX_BYTES = ([^;]+);", src())
        self.assertIsNotNone(m)
        self.assertEqual(eval(m.group(1).replace("*", "*")), 8 * 1024 ** 3,
                         "the chunked ceiling changed — if that was deliberate, re-run "
                         "scripts/measure_chunked_upload.py and update the reasoning above it")

    def test_the_unchunked_ceiling_stays_small(self):
        """A platform with no slice I/O still holds the whole file, its ciphertext AND the upload
        body at once, and an Android WebView has far less headroom than Electron. This one is not
        about the server at all, so the measurement above does not license raising it."""
        m = re.search(r"const SYNC_MAX_UNCHUNKED = ([^;]+);", src())
        self.assertIsNotNone(m)
        self.assertEqual(eval(m.group(1)), 32 * 1024 * 1024)


class TestTheServerLimitIsPerUploadNotPerFile(unittest.TestCase):
    def test_a_chunked_platform_is_not_capped_by_the_per_request_limit(self):
        body = src()
        i = body.index("async function maxBytes()")
        fn = body[i:i + 1400]
        self.assertIn("if(chunked)", fn,
                      "maxBytes must branch on chunking: with chunks the server's per-upload limit "
                      "bounds a CHUNK, not the file")
        # Just the chunked block: a fixed-size window spills into the unchunked line below it, where
        # Math.min IS correct — which is how the first version of this test failed on working code.
        block = fn.split("if(chunked){", 1)[1].split("\n    }", 1)[0]
        self.assertNotRegex(
            block, r"Math\.min\(server",
            "the chunked branch must not take the lower of the server limit and the file ceiling — "
            "that is the conflation this test exists for")

    def test_a_server_limit_smaller_than_a_chunk_still_binds(self):
        """The one case where the server's number really is the file's limit: if a single chunk
        would not fit in one request, nothing larger can be sent at all."""
        body = src()
        i = body.index("async function maxBytes()")
        self.assertIn("server < chunk", body[i:i + 1400])

    def test_an_unchunked_platform_is_still_capped_by_both(self):
        body = src()
        i = body.index("async function maxBytes()")
        self.assertIn("Math.min(server, SYNC_MAX_UNCHUNKED)", body[i:i + 1400])

    def test_the_chunk_fallback_matches_what_the_sweep_uses(self):
        """`chunkAbove` in the sweep and CHUNK_FALLBACK here have to mean the same thing, or a
        platform that reports no chunk size is sized against a chunk length nothing ever writes.

        `chunkAbove` is no longer a literal: it is the PLATFORM's own chunk size, because it used to
        be hardcoded to the desktop's 16 MB while Android's is 4 MB — so every file in between was
        held whole on the device with the least headroom, which is the renderer dying mid-sweep. The
        rule this test exists for is unchanged and is now checked where it actually applies: the
        fallback, taken when a platform reports no chunk size at all, must still be CHUNK_FALLBACK."""
        body = src()
        m = re.search(r"const CHUNK_FALLBACK = ([^;]+);", body)
        self.assertIsNotNone(m)
        above = re.search(r"chunkAbove: ([^\n]+),", body)
        self.assertIsNotNone(above)
        expr = above.group(1).strip()
        self.assertIn("chunkBytes", expr,
                      "chunkAbove is a fixed number again: %s" % expr)
        self.assertTrue(expr.endswith("CHUNK_FALLBACK"),
                        "a platform that reports no chunk size falls back to something other than "
                        "CHUNK_FALLBACK: %s" % expr)


if __name__ == "__main__":
    unittest.main()
