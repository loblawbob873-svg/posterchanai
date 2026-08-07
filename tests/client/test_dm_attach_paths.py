"""Every way a file gets into a DM must obey the 🔒, not just the one that was built first.

This test exists because the feature shipped covering ONE path. There are four:

    📎 file picker    uploads   -> encrypt
    paste an image    uploads   -> encrypt
    🌸 drive picker   inserts a URL to a blob ALREADY on Blossom in the clear
    🎬 GIF search     inserts someone else's URL on someone else's server

The middle two never call uploadBlob, so they sailed straight past a lock that only wrapped the
upload. A lit padlock over a world-readable link is worse than no padlock: the first tells you
you're safe. The GIF case cannot be made private at all, so the only honest behaviour is to say so.

Asserted against the SOURCE — these are wiring facts (which function each control calls), and the
failure mode is a control quietly kept pointing at the plaintext path.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")


class DmAttachPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(APP).read()

    def _one(self, pattern, what):
        found = re.findall(pattern, self.src)
        self.assertTrue(found, f"could not find {what} — the wiring moved; re-point this test")
        return found

    def _blocks(self, anchor, span=900):
        """Every occurrence of `anchor` plus the code that follows it.

        Deliberately a slice, not a clever pattern: matching a JS handler with a regex is how the
        first draft of this test passed while looking at nothing but a declaration line."""
        out, i = [], self.src.find(anchor)
        while i >= 0:
            out.append(self.src[i:i + span])
            i = self.src.find(anchor, i + 1)
        self.assertTrue(out, f"could not find {anchor!r} — the wiring moved; re-point this test")
        return out

    # --- 📎 the file picker, both composers ------------------------------------------------------

    def test_the_file_picker_encrypts_in_both_composers(self):
        """The thread composer AND the new-conversation modal. The modal was missed the first time,
        which meant a FIRST message — the one most likely to carry something private — uploaded in
        the clear no matter what the lock said."""
        for anchor in ("#dm-file',root).onchange=", "#dm-file').onchange="):
            h = self._blocks(anchor)[0]
            self.assertIn("uploadSharedEnc", h, f"{anchor} never encrypts")
            self.assertIn("dmEncOn()", h, f"{anchor} does not consult the lock")

    def test_the_public_post_composer_never_encrypts(self):
        """The same shape of handler backs #cmp-file, and a public note encrypted to nobody is just a
        broken image for every reader. Asserted because the DM and post composers are near-identical
        code a few hundred lines apart — the easy mistake is to 'fix' both."""
        h = self._blocks("#cmp-file',root).onchange=")[0]
        self.assertNotIn("uploadSharedEnc", h)
        self.assertNotIn("dmEncOn", h)

    # --- paste -----------------------------------------------------------------------------------

    def test_pasting_an_image_encrypts(self):
        """Paste-to-attach uploads exactly like 📎 does, so it has exactly the same duty."""
        block = self._blocks("for(const f of files){ const _e=encHere();", 400)[0]
        self.assertIn("uploadSharedEnc", block, "paste does not encrypt when the lock is on")
        self.assertIn("uploadBlob", block, "paste must still upload in the clear when the lock is off")

    def test_paste_encryption_is_opt_in_per_composer(self):
        """wireImgAttach also backs composers where encrypting would be wrong — a public post
        encrypted to nobody is a broken image. So the DM boxes flag themselves; nothing else does."""
        self.assertIn("const encHere = () => !!(opts && opts.enc) && dmEncOn();", self.src)
        # Call sites only — skip the definition. `[^)]*` cannot cross the ')' in $('#dm-atts'), which
        # is why the first version of this matched nothing and passed vacuously.
        calls = [c for c in re.findall(r"wireImgAttach\((.+?)\);", self.src) if not c.startswith("inp, strip")]
        self.assertEqual(len(calls), 2, f"expected the two DM composers, got {calls!r}")
        for call in calls:
            self.assertIn("enc:true", call, f"this composer would not encrypt a pasted image: {call!r}")

    # --- 🌸 the drive picker ---------------------------------------------------------------------

    def test_the_drive_picker_does_not_insert_a_public_link_under_a_lit_lock(self):
        """THE ONE THAT SHIPPED BROKEN. It inserts a URL to a blob already public on Blossom, so it
        never uploaded and never met the lock — a plaintext PNG from eleven hours earlier rendered
        perfectly on a client that cannot decrypt anything."""
        self.assertIn("function dmPickMedia(", self.src)
        body = re.search(r"function dmPickMedia\(inp\)\{(.*?)\n  \}", self.src, re.S).group(1)
        self.assertIn("encryptExistingUrl", body, "the drive picker must re-upload encrypted")
        self.assertIn("dmEncOn()", body, "the drive picker must consult the lock")
        for site in self._one(r"#dm-files'(?:,root)?\)\.onclick=(\S+)", "the 🌸 buttons"):
            self.assertTrue(site.startswith("dmPickMedia"),
                            f"a 🌸 button still calls {site} instead of the lock-aware picker")

    def test_the_drive_picker_admits_the_original_is_still_public(self):
        """Re-uploading encrypted keeps the LINK private; it cannot un-publish the copy already on
        the drive. Implying otherwise is the failure this whole feature is about."""
        body = re.search(r"function dmPickMedia\(inp\)\{(.*?)\n  \}", self.src, re.S).group(1)
        self.assertRegex(body, r"still public", "the toast must not imply the original is now private")

    def test_already_encrypted_drive_content_is_not_double_wrapped(self):
        """A file from an encrypted folder is master-key ciphertext. Wrapping it again leaves the
        recipient able to peel one layer and holding ciphertext they have no key for."""
        body = re.search(r"function dmPickMedia\(inp\)\{(.*?)\n  \}", self.src, re.S).group(1)
        self.assertIn("octet-stream", body)

    # --- 🎬 GIFs ----------------------------------------------------------------------------------

    def test_gifs_say_they_are_not_encrypted_rather_than_pretending(self):
        """A GIF is a link to another server. There is nothing to encrypt, so the lock must not
        silently imply it covered one."""
        self.assertIn("function dmPickGif(", self.src)
        body = re.search(r"function dmPickGif\(inp\)\{(.*?)\n  \}", self.src, re.S).group(1)
        self.assertIn("dmEncOn()", body)
        self.assertIn("not encrypted", body)
        for site in self._one(r"#dm-gif'(?:,root)?\); if\(g\) g\.onclick=(\S+?);", "the 🎬 buttons"):
            self.assertTrue(site.startswith("dmPickGif"),
                            f"a 🎬 button still calls {site} instead of the lock-aware picker")

    # --- the lock itself --------------------------------------------------------------------------

    def test_one_definition_of_whether_the_lock_is_on(self):
        """Four call sites, one reader. Two spellings of "is it on" is how one of them drifts."""
        self.assertIn("const dmEncOn = () => !!ClientSettings.get('dmEncryptAtts');", self.src)
        # nothing else may read the raw setting
        raw = re.findall(r"ClientSettings\.get\('dmEncryptAtts'\)", self.src)
        self.assertEqual(len(raw), 1, "read the setting through dmEncOn(), not directly")


if __name__ == "__main__":
    unittest.main()
