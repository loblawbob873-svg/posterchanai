"""Regression tests for the fediverse → Nostr MIRROR visibility guard (privacy leak prevention).

Run: venv-unified/bin/python -m unittest tests.test_fedi_mirror_visibility

The invariant: ONLY a public-audience fediverse status may become a public Nostr kind-1. The guard is
an ALLOWLIST (`_is_public_audience`) — public / unlisted / home — shared by the timeline mirror
(_deliver) AND the personal-notification plane, so a private/DM/followers-only/Misskey-specified or
unknown/blank visibility can never be leaked to the public firehose. This replaced a blocklist
(`in ("direct","private")`) that only knew the Mastodon/Pleroma vocabulary and would have leaked
Misskey `followers`/`specified` once that path is enabled.
"""
import unittest

from app.services.fedi_nostr_bridge_service import _is_public_audience


class TestPublicAudience(unittest.TestCase):
    def test_public_audiences_allowed(self):
        for v in ("public", "unlisted", "home", "PUBLIC", "Unlisted"):
            self.assertTrue(_is_public_audience({"visibility": v}), f"{v!r} should be public")

    def test_mastodon_pleroma_private_blocked(self):
        for v in ("direct", "private", "list", "local"):
            self.assertFalse(_is_public_audience({"visibility": v}), f"{v!r} must NOT be public")

    def test_misskey_nonpublic_blocked(self):
        # the latent leak this fix closes ahead of enabling the Misskey path
        for v in ("followers", "specified"):
            self.assertFalse(_is_public_audience({"visibility": v}), f"misskey {v!r} must NOT be public")

    def test_missing_or_blank_is_not_public(self):
        # abnormal (every real API sets visibility on mirrored statuses) → fail closed, don't leak
        self.assertFalse(_is_public_audience({}))
        self.assertFalse(_is_public_audience({"visibility": None}))
        self.assertFalse(_is_public_audience({"visibility": ""}))

    def test_unknown_value_is_not_public(self):
        self.assertFalse(_is_public_audience({"visibility": "some_new_scope"}))


if __name__ == "__main__":
    unittest.main()
