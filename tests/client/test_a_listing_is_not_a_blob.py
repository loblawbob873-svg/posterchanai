"""A Blossom LISTING must never be cached as a content-addressed blob.

`GET <server>/list/<pubkey>` ends in 64 hex characters, because a pubkey is 64 hex characters —
exactly like a sha256. The service worker's `isDriveBlob` tested only "does the path end in 64 hex",
so every listing the client made was handed to `cacheFirstBlob`: served from an immutable cache on
every later call, and downloaded a SECOND time in the background to store it.

On the reporting deployment that listing is 37,483 descriptors / 9.7 MB and changes on every upload.
The user-visible shape was never "slow" — it was a file that uploaded successfully and never
appeared, on Files and in the wallpaper picker, because both were being shown a listing cached from
before the upload.

This runs the SHIPPED predicate out of sw.js under node. It is written to fail on the pre-fix rule:
`test_a_listing_is_never_a_blob` is exactly the assertion the old regex could not satisfy.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

SW = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "sw.js"


def _run(cases):
    """Lift isDriveBlob (and the route set it reads) out of the shipped worker and run it."""
    src = SW.read_text()

    routes = re.search(r"const _NOT_A_BLOB_ROUTE = new Set\(\[[^\]]*\]\);", src)
    fn = re.search(r"function isDriveBlob\(url, req\)\{.*?\n\}", src, re.S)
    if not fn:
        raise AssertionError("isDriveBlob not found in sw.js")

    harness = (routes.group(0) if routes else "") + "\n" + fn.group(0) + "\n" + (
        "const cases = " + json.dumps(cases) + ";\n"
        "const out = cases.map(c => isDriveBlob(new URL(c.url), "
        "{mode: c.mode || 'cors', destination: c.destination || ''}));\n"
        "console.log(JSON.stringify(out));\n"
    )
    p = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + p.stderr[:400])
    return json.loads(p.stdout.strip().splitlines()[-1])


PUBKEY = "4b56bbf41c92e586" + "a" * 48          # 64 hex, the shape of a real pubkey
SHA = "38f56beebe3f525ea2d8448aabef17d5e868a73f108839a176aeda216b800d9b"


class ListingIsNotABlob(unittest.TestCase):
    def test_a_listing_is_never_a_blob(self):
        """The regression. A listing URL ends in 64 hex and must still be refused."""
        urls = [
            "https://media.poster.place/list/" + PUBKEY,
            "https://media.poster.place/list/" + PUBKEY + "?limit=50",
            "https://poster.place/blossom/list/" + PUBKEY,
            "https://media.poster.place/LIST/" + PUBKEY.upper(),
        ]
        got = _run([{"url": u} for u in urls])
        for u, v in zip(urls, got):
            self.assertFalse(v, "a listing was classified as a cacheable blob: " + u)

    def test_a_real_blob_is_still_a_blob(self):
        """The fix must not cost the cache it exists for — encrypted drive reads."""
        urls = [
            "https://media.poster.place/" + SHA,
            "https://media.poster.place/" + SHA + ".png",
            "https://poster.place/blossom/" + SHA,
            "https://my.example/some/deep/mount/" + SHA,
        ]
        got = _run([{"url": u} for u in urls])
        for u, v in zip(urls, got):
            self.assertTrue(v, "a real drive blob stopped being cacheable: " + u)

    def test_the_old_exclusions_are_intact(self):
        """Navigations and <img>/<video> keep going to their own paths, not the blob cache."""
        base = "https://media.poster.place/" + SHA
        got = _run([
            {"url": base, "mode": "navigate"},
            {"url": base, "destination": "image"},
            {"url": base, "destination": "video"},
        ])
        self.assertEqual(got, [False, False, False])

    def test_the_poisoned_cache_is_swept_on_activate(self):
        """Fixing the predicate cannot un-poison an installed client; a sweep must run."""
        src = SW.read_text()
        self.assertIn("_purgeMisfiledListings", src)
        act = re.search(r"self\.addEventListener\('activate'.*?\n\}\);", src, re.S)
        self.assertIsNotNone(act, "activate handler not found")
        self.assertIn("_purgeMisfiledListings", act.group(0),
                      "the sweep exists but activate never calls it")


if __name__ == "__main__":
    unittest.main()
