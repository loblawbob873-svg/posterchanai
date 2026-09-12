"""The Music folder must be reachable from the Meme Builder's file picker.

MEASURED on a real desktop before the fix: the builder's picker listed 22 folders — every folder on
the drive except Music. Reported as "In meme builder, i can't fucking choose music as a folder
choice!" and again as "i could not browse the Music folder in Meme builder for example in the Files
picker".

Cause: `FilesIdx.isEncFolder()` answers TRUE for the literal name 'Music' — correct, because tracks
are stored encrypted — and every picker filtered its folder bar with `!isEncFolder(f)`. So Music was
missing from EVERY picker in the app, not just this one.

That also contradicted a decision recorded in meme.js: the dedicated "🎵 Music or a voice-over"
entry was removed on the grounds that "music is where every other file is ... pickBlossom's filter
already allows it". It did not.

The second half matters as much as the first: `POST /client/meme/render` sends `layers[].src` and
the SERVER fetches it, so handing the builder a ciphertext URL — or a decrypted `blob:` URL — gives
a track that fails at render. Offering the folder without that is a worse bug than hiding it.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"


def _code_only(src):
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "/*":
            j = src.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append(" " * (j - i)); i = j
        elif two == "//":
            j = src.find("\n", i); j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        else:
            out.append(src[i]); i += 1
    return "".join(out)


class MusicInTheBuilder(unittest.TestCase):
    def setUp(self):
        self.app = _code_only((ROOT / "app.js").read_text())
        self.meme = _code_only((ROOT / "meme.js").read_text())
        m = re.search(r"function pickBlossom\(\)\{.*?\n  \}", self.meme, re.S)
        self.assertIsNotNone(m, "pickBlossom not found in meme.js")
        self.pick = m.group(0)

    def test_music_is_still_classified_as_encrypted(self):
        """The premise. If this ever stops being true the fix is pointless, not wrong."""
        self.assertRegex(self.app, r"isEncFolder\(name\)\{[^}]*name\s*===\s*'Music'",
                         "Music is no longer treated as an encrypted folder — re-check this fix")

    def test_the_builder_asks_for_encrypted_folders(self):
        """The regression: without opting in, the Music folder is not in the sheet at all."""
        self.assertRegex(self.pick, r"allowEncrypted:\s*true",
                         "the Meme Builder picker does not ask for encrypted folders, so Music is hidden")

    def test_the_folder_bar_honours_the_opt_in(self):
        """The filter that was hiding it."""
        self.assertRegex(self.app, r"filter\(f=>opts\.allowEncrypted\|\|!FilesIdx\.isEncFolder\(f\)\)",
                         "the picker's folder bar still hides every encrypted folder unconditionally")

    def test_the_blobs_inside_it_are_shown_too(self):
        """Showing the folder and then an empty grid is not a fix — every Music blob is enc:true."""
        self.assertIn("m.enc && !opts.allowEncrypted", self.app,
                      "encrypted blobs are still filtered out regardless of the caller's opt-in")

    def test_every_other_caller_is_unchanged(self):
        """A composer publishes a URL; it must never be handed ciphertext nobody else can fetch."""
        self.assertNotIn("allowEncrypted: true", self.app,
                         "something in app.js opted a URL-publishing caller into encrypted files")

    def test_a_picked_track_is_made_fetchable_before_it_becomes_a_layer(self):
        """The server fetches layers[].src, so a blob: URL previews and then fails at render."""
        self.assertIn("encFileUrl", self.pick, "the builder never decrypts the file it picked")
        self.assertIn("addMediaFiles", self.pick,
                      "the decrypted track does not go through the upload path a local file uses")
        # and it must not shortcut straight to a layer with the ciphertext url
        enc_branch = self.pick[self.pick.index("encFileUrl"):]
        self.assertIn("return", enc_branch,
                      "the encrypted branch falls through and adds the ciphertext URL as a layer")

    def test_the_caller_is_told_which_file_and_whether_it_is_encrypted(self):
        """Without the sha there is nothing to decrypt from."""
        self.assertRegex(self.app, r"onPick\(\{url, type, ext, name, sha, enc:isEnc\}\)",
                         "the picker does not tell its caller the hash or the encrypted flag")
        self.assertIn('data-sha=', self.app, "picker cards carry no sha to read back")

    def test_it_says_what_it_is_doing(self):
        """It puts a plaintext copy on the drive; that must not happen silently."""
        self.assertIn("toast", self.pick, "the re-upload is silent")


if __name__ == "__main__":
    unittest.main()
