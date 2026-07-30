"""A custom emoji is served with the Content-Type its BYTES say, never a generic one.

Run: venv-unified/bin/python -m unittest tests.test_emoji_media_type

An akkoma pack.json maps a shortcode to whatever filename it likes, and some entries have no
extension at all (11 of the 3336 in the pack this was built against). mimetypes.guess_type returns
nothing for those, they went out as `application/octet-stream`, and the emoji route also sends
`X-Content-Type-Options: nosniff` — correctly, they are operator-uploaded files on a public,
unauthenticated path. A strict browser then REFUSES to draw them.

That combination is nasty because of how partial the failure is:
  * Firefox honours nosniff for images and showed nothing; Chrome sniffs anyway and showed the
    emoji — so it was broken "only for some people".
  * The picker grid asks for `?t=1`, which is a generated image/webp thumbnail, so the PICKER
    looked perfect while the full-size image behind it did not load.
  * The Meme Builder renderer reads the bytes server-side, so a sticker came out correct in the
    exported meme and was an empty box in the preview — the report was "it renders in the final
    project but the preview is blank", which sounds like a client bug and is not one.

So: assert the sniffing, and assert that no emoji on THIS deployment is served generically.
"""
import os
import tempfile
import unittest

from app.services import emoji_service


# Smallest headers that are still unambiguous — media_type only ever reads the first 32 bytes.
SAMPLES = {
    "image/png":  b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
    "image/gif":  b"GIF89a" + b"\x00" * 16,
    "image/jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 16,
    "image/webp": b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8,
    "image/bmp":  b"BM" + b"\x00" * 20,
    "image/avif": b"\x00\x00\x00\x20ftypavif" + b"\x00" * 12,
}


class TestEmojiMediaType(unittest.TestCase):
    def _write(self, data, name):
        d = tempfile.mkdtemp()
        p = os.path.join(d, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_sniffs_every_format_with_no_extension(self):
        # The whole point: the filename says nothing, so the header has to.
        for mime, data in SAMPLES.items():
            with self.subTest(mime=mime):
                self.assertEqual(emoji_service.media_type(self._write(data, "dogsmile")), mime)

    def test_extension_is_used_when_it_is_there(self):
        # The cheap path still wins — no file read needed for the other 3325.
        self.assertEqual(emoji_service.media_type(self._write(SAMPLES["image/png"], "a.png")), "image/png")
        self.assertEqual(emoji_service.media_type(self._write(SAMPLES["image/webp"], "a.webp")), "image/webp")

    def test_a_lying_extension_loses_to_the_bytes(self):
        # guess_type only wins when it returns an image/* type, and a .txt does not — so a GIF saved
        # as "foo.txt" is still served as a GIF rather than as text/plain.
        self.assertEqual(emoji_service.media_type(self._write(SAMPLES["image/gif"], "foo.txt")), "image/gif")

    def test_svg_is_recognised(self):
        self.assertEqual(emoji_service.media_type(self._write(b'<svg xmlns="x"/>', "s")), "image/svg+xml")
        self.assertEqual(emoji_service.media_type(self._write(b'<?xml version="1.0"?><svg/>', "s")), "image/svg+xml")

    def test_unreadable_or_unknown_stays_generic(self):
        # No guessing games: something that is not an image must NOT be labelled as one.
        self.assertEqual(emoji_service.media_type(self._write(b"not an image at all", "x")),
                         "application/octet-stream")
        self.assertEqual(emoji_service.media_type("/nonexistent/path/x"), "application/octet-stream")

    def test_no_emoji_on_this_deployment_is_served_generically(self):
        """The end-to-end assertion. Skips where there is no emoji directory (CI, a fresh node)."""
        entries = emoji_service.index()
        if not entries:
            self.skipTest("this deployment has no custom emoji installed")
        bad = [e["shortcode"] for e in entries
               if not emoji_service.media_type(e["path"]).startswith("image/")]
        self.assertEqual(bad, [], "these emoji would be served as a non-image and a strict browser "
                                  "would refuse to draw them: %s" % bad[:20])


if __name__ == "__main__":
    unittest.main()
