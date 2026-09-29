"""An emoji URL in an already-published note keeps working after its pack is split or renamed.

Run: venv-unified/bin/python -m unittest tests.test_emoji_moved

Notes carry `<base>/<pack>/<shortcode>.<ext>` in their NIP-30 tags forever. When DRC_emojo was split
into themed packs (pepe/, anime/, letters/ …) 358 notes still pointed at DRC_emojo/…, and the route
looked emoji up strictly by (pack, shortcode), so every one of them would have become a 404.
"""
import os
import tempfile
import unittest
from unittest import mock

from app.services import emoji_service

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class TestEmojiMoved(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "pepe"))
        with open(os.path.join(self.root, "pepe", "pepe_pantsu.png"), "wb") as fh:
            fh.write(PNG)
        p = mock.patch.object(emoji_service, "emoji_dir", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        emoji_service._invalidate()
        self.addCleanup(emoji_service._invalidate)

    def test_exact_pack_still_resolves(self):
        self.assertEqual(emoji_service.lookup("pepe", "pepe_pantsu")["pack"], "pepe")

    def test_old_pack_name_finds_the_emoji_where_it_lives_now(self):
        self.assertIsNone(emoji_service.lookup("DRC_emojo", "pepe_pantsu"))
        e = emoji_service.lookup_moved("pepe_pantsu")
        self.assertEqual((e["pack"], e["shortcode"]), ("pepe", "pepe_pantsu"))

    def test_unknown_shortcode_is_still_a_miss(self):
        self.assertIsNone(emoji_service.lookup_moved("no_such_emoji"))


if __name__ == "__main__":
    unittest.main()
