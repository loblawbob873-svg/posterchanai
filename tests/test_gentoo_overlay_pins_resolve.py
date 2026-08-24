"""The PosterChanOS overlay points at files that still exist.

Run: venv-unified/bin/python -m pytest tests/test_gentoo_overlay_pins_resolve.py

`app-misc/posterchan-desktop` carries a Manifest, so portage needs the EXACT bytes it was written
against. The desktop workflow deletes and re-creates the rolling `desktop-latest` release on every
build, which takes every previously-versioned tarball with it — so an ebuild fetching from that tag
404s on the next desktop build. Not a checksum failure: nothing to download at all, and
`emerge posterchan-desktop` cannot run.

It went stale that way three times (1.0.818, 1.0.825, 1.0.828) and each time it was found by a
person rather than by anything here. What is asserted:

  * the ebuild does not fetch from a rolling tag;
  * the ebuild, its filename and its Manifest all name the SAME version — the mismatch that
    "os: match the desktop Manifest to the 1.0.818 ebuild" was fixing by hand;
  * the Manifest is well-formed (size + both digests), because portage refuses outright without it.

The live half — that the URL still resolves — needs the network and is checked by
`scripts/check_gentoo_overlay.py`, which is where a network-dependent assertion belongs.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchan-desktop")


def _ebuild():
    names = [f for f in os.listdir(PKG) if f.endswith(".ebuild")]
    if len(names) != 1:
        raise AssertionError(f"expected exactly one desktop ebuild, found {names}")
    return names[0]


class TheOverlayPinsSomethingThatExists(unittest.TestCase):
    def setUp(self):
        self.name = _ebuild()
        self.pv = self.name[len("posterchan-desktop-"):-len(".ebuild")]
        with open(os.path.join(PKG, self.name), encoding="utf-8") as fh:
            self.src = fh.read()
        with open(os.path.join(PKG, "Manifest"), encoding="utf-8") as fh:
            self.manifest = fh.read().strip()

    def test_it_does_not_fetch_from_the_rolling_release(self):
        """`desktop-latest` is deleted and re-created on every desktop build."""
        uri = re.search(r'(?m)^SRC_URI="([^"]+)"', self.src)
        self.assertTrue(uri, "no SRC_URI — re-point this test")
        self.assertNotIn("/desktop-latest/", uri.group(1),
                         "the overlay pins a Manifest against a tag whose assets are deleted on "
                         "every desktop build, so this 404s the next time the desktop is released")
        self.assertIn("desktop-v${PV}", uri.group(1),
                      "the per-version tag is what makes a pin permanent")

    def test_the_ebuild_its_filename_and_its_manifest_agree(self):
        rows = [r for r in self.manifest.splitlines() if r.strip()]
        self.assertEqual(len(rows), 1, f"expected one DIST row, got {len(rows)}")
        parts = rows[0].split()
        self.assertEqual(parts[0], "DIST")
        self.assertEqual(parts[1], f"posterchan-desktop-{self.pv}.tar.zst",
                         f"the Manifest names a different version from the ebuild ({self.pv}) — "
                         "portage then fetches one file and verifies another")

    def test_the_manifest_carries_a_size_and_both_digests(self):
        """Without them portage refuses outright: 'VERIFY FAILED! Reason: Insufficient data for
        checksum verification'."""
        parts = self.manifest.split()
        self.assertTrue(parts[2].isdigit() and int(parts[2]) > 1_000_000,
                        f"implausible DIST size: {parts[2]}")
        body = " ".join(parts[3:])
        self.assertRegex(body, r"BLAKE2B [0-9a-f]{128}\b")
        self.assertRegex(body, r"SHA512 [0-9a-f]{128}\b")

    def test_ci_publishes_the_tag_the_ebuild_pins(self):
        """A pin nothing produces is a pin that breaks the first time it is used."""
        with open(os.path.join(ROOT, ".github", "workflows", "desktop.yml"), encoding="utf-8") as fh:
            wf = fh.read()
        self.assertIn("tag_name: desktop-v1.0.${{ github.run_number }}", wf,
                      "nothing creates the per-version release the ebuild fetches from")
        self.assertIn("PosterChan-*-linux-x64.tar.zst", wf,
                      "the per-version release does not carry the tarball the overlay wants")


if __name__ == "__main__":
    unittest.main()
