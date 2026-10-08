"""The PosterChan emoji pack ships with PosterChan ("we need a small set of posterchan emoji pack").

assets/emoji/ is each node's own collection and is gitignored, so a pack meant for EVERY install lives in
assets/emoji-builtin/ and the emoji index adds it after the operator's packs. Checked as a person meets it:
the pack is listed, its emoji are served by the same route a published note's NIP-30 tag points at, an
operator's own shortcode still wins, a node with custom emoji switched off has none, and the files of a
built-in emoji can't be renamed or deleted from Admin (they are part of the code checkout).
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from app.services import emoji_service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK = os.path.join(ROOT, "assets", "emoji-builtin", "posterchan")
FACES = ["pc_neutral", "pc_happy", "pc_excited", "pc_blink", "pc_wink", "pc_thinking", "pc_surprised",
         "pc_shy", "pc_determined", "pc_sleepy"]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class TheShippedArt(unittest.TestCase):
    def test_every_face_is_a_128px_cut_out(self):
        for sc in FACES:
            im = Image.open(os.path.join(PACK, sc + ".png"))
            self.assertEqual(im.size, (128, 128), sc)
            self.assertEqual(im.mode, "RGBA", sc)
            self.assertEqual(im.getextrema()[3][0], 0, f"{sc} has no transparent background")
            self.assertGreater(sum(1 for a in im.getchannel("A").getdata() if a > 128), 128 * 128 * 0.3,
                               f"{sc} is mostly empty")

    def test_the_dance_is_animated_and_small(self):
        im = Image.open(os.path.join(PACK, "pc_dance.webp"))
        self.assertGreaterEqual(getattr(im, "n_frames", 1), 8)
        self.assertLess(os.path.getsize(os.path.join(PACK, "pc_dance.webp")), 200_000)


class ThePackOnANode(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.patch = mock.patch.object(emoji_service, "emoji_dir", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        emoji_service._invalidate()
        self.addCleanup(emoji_service._invalidate)

    def test_a_node_with_no_packs_of_its_own_has_the_posterchan_pack(self):
        got = {e["shortcode"]: e for e in emoji_service.index(force=True) if e["pack"] == "posterchan"}
        self.assertTrue(set(FACES + ["pc_dance"]) <= set(got), sorted(got))
        self.assertTrue(all(e.get("builtin") for e in got.values()))
        pack = [p for p in emoji_service.packs() if p["name"] == "posterchan"]
        self.assertEqual(len(pack), 1)
        self.assertTrue(pack[0]["builtin"])

    def test_it_is_served_by_the_route_a_note_points_at(self):
        from app.routers.client import client_emoji_file
        r = asyncio.run(client_emoji_file("posterchan", "pc_happy.png"))
        self.assertEqual(os.path.normpath(r.path), os.path.normpath(os.path.join(PACK, "pc_happy.png")))
        self.assertEqual(r.media_type, "image/png")
        r = asyncio.run(client_emoji_file("posterchan", "pc_dance.webp"))
        self.assertEqual(r.media_type, "image/webp")

    def test_the_operators_own_shortcode_wins(self):
        os.makedirs(os.path.join(self.root, "mine"))
        with open(os.path.join(self.root, "mine", "pc_happy.png"), "wb") as fh:
            fh.write(PNG)
        emoji_service._invalidate()
        self.assertEqual(emoji_service.lookup_moved("pc_happy")["pack"], "mine")
        self.assertEqual(emoji_service.lookup_moved("pc_wink")["pack"], "posterchan")

    def test_built_in_emoji_cannot_be_renamed_or_deleted(self):
        with self.assertRaises(ValueError):
            emoji_service.delete_emoji("posterchan", "pc_happy")
        with self.assertRaises(ValueError):
            emoji_service.rename_emoji("posterchan", "pc_happy", "pc_glad")
        self.assertTrue(os.path.isfile(os.path.join(PACK, "pc_happy.png")), "a built-in file was removed")


class EmojiSwitchedOff(unittest.TestCase):
    def test_a_node_with_custom_emoji_off_has_none_built_in_either(self):
        with mock.patch.object(emoji_service, "emoji_dir", return_value=""):
            emoji_service._invalidate()
            try:
                self.assertEqual([e for e in emoji_service.index(force=True) if e.get("builtin")], [])
            finally:
                emoji_service._invalidate()


if __name__ == "__main__":
    unittest.main()


class OverwritingABuiltIn(unittest.TestCase):
    """Code review: an Admin upload with Overwrite onto a built-in emoji went down the plain-pack branch and
    os.remove()d assets/emoji-builtin/posterchan/<file> -- a tracked file, which the next deploy's `git commit -a`
    would have deleted on every node. Run against a COPY of the built-in dir so the real one is never at risk."""

    def test_the_operator_gets_their_own_copy_and_the_built_in_file_survives(self):
        import shutil
        root = tempfile.mkdtemp()
        builtin = tempfile.mkdtemp()
        shutil.copytree(os.path.dirname(PACK), builtin, dirs_exist_ok=True)
        buf = __import__("io").BytesIO(); Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(buf, "PNG")
        with mock.patch.object(emoji_service, "emoji_dir", return_value=root), \
             mock.patch.object(emoji_service, "BUILTIN_DIR", builtin):
            emoji_service._invalidate()
            try:
                self.assertTrue(emoji_service.lookup("posterchan", "pc_happy")["builtin"])
                emoji_service.add_emoji("posterchan", "pc_happy", "pc_happy.png", buf.getvalue(), overwrite=True)
                emoji_service._invalidate()
                e = emoji_service.lookup("posterchan", "pc_happy")
                self.assertFalse(e.get("builtin"), "the operator's upload did not take over the shortcode")
                self.assertTrue(e["path"].startswith(root), e["path"])
            finally:
                emoji_service._invalidate()
        self.assertTrue(os.path.isfile(os.path.join(builtin, "posterchan", "pc_happy.png")), "the built-in file was deleted")
        shutil.rmtree(root, ignore_errors=True); shutil.rmtree(builtin, ignore_errors=True)
