"""A private key must never be stored as a Concord invite.

A real bot was created with its `nsec1…` in BOTH `nostr_nsec` and `concord_invite` — byte-identical,
same SHA. Nothing objected, so the bot had no invite and never joined its room: "i created a new bot
with existing nsec and it never joined the concord room, wtf now".

The silent join failure is the visible half. The half that matters more is that a PRIVATE KEY sat in
a field the code treats as a room link: handed to the CORD parser, exported as `CONCORD_INVITE`, and
redisplayed in the form. However it got there — a mis-paste, or a password manager filling two boxes
that look alike — nothing should have accepted it.

Checked on the SERVER so it holds for the API and the migration seed, not only the form.
"""
import unittest
from fastapi import HTTPException

from app.routers.bots import _vet_config

GOOD = "https://poster.place/invite/naddr1qvzqqqyzz5pzp#BAADAQIAGG5vc3Ry"


class TheInviteFieldIsVetted(unittest.TestCase):

    def _refused(self, cfg):
        with self.assertRaises(HTTPException) as e:
            _vet_config(cfg)
        self.assertEqual(e.exception.status_code, 400)
        return str(e.exception.detail)

    def test_an_nsec_is_refused_and_told_where_it_belongs(self):
        """THE REPORTED CASE, exactly as it was stored."""
        msg = self._refused({"concord_invite": "nsec1hjzgj7fklzqnu3kpx0jjva2mn349gu6gthxz8"})
        self.assertIn("private key", msg)
        self.assertIn("nsec", msg)

    def test_an_encrypted_key_is_refused_too(self):
        self._refused({"concord_invite": "ncryptsec1qgg9947rlpvqu76pj5ecreduf9jxhselq2nae2kghhvd5g"})

    def test_an_invite_without_its_fragment_is_refused(self):
        """The `#` part IS the room's decryption secret. Without it the bot opens the link and can
        never read the room — which is the silent failure this whole area keeps producing."""
        msg = self._refused({"concord_invite": "https://poster.place/invite/naddr1qvzqqqyzz5pzp"})
        self.assertIn("#", msg)

    def test_arbitrary_text_is_refused(self):
        self._refused({"concord_invite": "the lounge room"})

    def test_a_real_invite_passes_through_untouched(self):
        out = _vet_config({"concord_invite": GOOD, "nostr_nsec": "nsec1abc"})
        self.assertEqual(out["concord_invite"], GOOD)
        self.assertEqual(out["nostr_nsec"], "nsec1abc")

    def test_no_invite_at_all_is_fine(self):
        """Most bots are not in a room; this must not become a required field."""
        self.assertEqual(_vet_config({"nostr_nsec": "nsec1abc"})["nostr_nsec"], "nsec1abc")
        self.assertEqual(_vet_config({}), {})
        self.assertEqual(_vet_config({"concord_invite": "   "})["concord_invite"], "   ")

    def test_both_write_paths_are_guarded(self):
        """Create AND update — an edit must not walk past the check."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app/routers/bots.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("_vet_config("), 3, "a config write path is unguarded")


if __name__ == "__main__":
    unittest.main()
