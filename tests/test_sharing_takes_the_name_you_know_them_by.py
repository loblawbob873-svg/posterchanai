"""SHARING MUST ACCEPT THE NAME THE OWNER ACTUALLY KNOWS THE PERSON BY.

The incident: the owner shared their media server with `matthew@poster.place` and he could not see
the library. matthew held a NIP-05 name granted by THIS node, had `can_media` ticked, and his
requests reached the right server — but the share box takes an npub or 64 hex characters and
nothing else, so the owner had no way to type the only name they know him by. They used the profile
permission instead, which grants the FEATURE and not the library, and the share never happened.
Measured afterwards on nas: the library's `shared_with` held one key, and it was somebody else's.

THE RULE, and the security boundary inside it:
  accepts-a-granted-name   a name in THIS node's registry resolves to its pubkey
  accepts-key-forms        npub and hex still work, unchanged
  refuses-a-name-we-did-not-grant   and says so, rather than silently sharing with nobody
  never-trusts-a-profile-claim   resolution reads `nostr_relay_nip05_names` — the registry this node
                           signs for — and NEVER a kind-0's self-declared `nip05`. Anyone can write
                           that claim into their own profile; honouring it here would hand a
                           stranger somebody else's library by typing a name they do not hold.

The registry is stubbed rather than read live, because `settings_store` returns nothing outside the
service environment — a probe that runs in a plain shell reports every name as ungranted, which is
how a test like this quietly proves nothing.
"""
import re
import unittest
from unittest import mock

from app.services import media_center as mc


GRANTED = {"matthew": "972f90195d13801bc33fb9585f04f39f6994f94977a03182883740010bbd33b5",
           "alice": "10621af37669cc51c540fa0a247312cfbf844710b6cf8e5f0a2b38315b251fca"}


def _with_registry(fn):
    """Stub the node's own NIP-05 registry, the only source resolution is allowed to use."""
    def run(*a, **k):
        with mock.patch("app.services.nostr_relay.thread._parse_nip05",
                        return_value=(dict(GRANTED), "")), \
             mock.patch("app.services.settings_store.get", return_value="ignored-by-the-stub"):
            return fn(*a, **k)
    return run


class SharingTakesTheNameYouKnowThemBy(unittest.TestCase):
    @_with_registry
    def test_a_granted_nip05_address_resolves(self):
        self.assertEqual(mc.resolve_share_key("matthew@poster.place"), GRANTED["matthew"])

    @_with_registry
    def test_the_bare_local_part_resolves_too(self):
        self.assertEqual(mc.resolve_share_key("matthew"), GRANTED["matthew"])

    @_with_registry
    def test_case_and_surrounding_space_do_not_matter(self):
        self.assertEqual(mc.resolve_share_key("  Matthew@Poster.Place  "), GRANTED["matthew"])

    @_with_registry
    def test_hex_and_npub_still_work(self):
        self.assertEqual(mc.resolve_share_key(GRANTED["alice"]), GRANTED["alice"])
        self.assertEqual(mc.resolve_share_key(GRANTED["alice"].upper()), GRANTED["alice"])

    @_with_registry
    def test_a_name_this_node_never_granted_is_refused_out_loud(self):
        with self.assertRaises(ValueError) as caught:
            mc.resolve_share_key("stranger@poster.place")
        self.assertIn("not granted", str(caught.exception).lower())

    @_with_registry
    def test_empty_is_refused_rather_than_sharing_with_nothing(self):
        for junk in ("", "   ", "@", "not a name!"):
            with self.assertRaises(ValueError):
                mc.resolve_share_key(junk)

    def test_resolution_never_reads_a_profiles_own_claim(self):
        """The security half. A kind-0's `nip05` is written by its subject and proves nothing."""
        import ast, inspect, textwrap
        src = inspect.getsource(mc.resolve_share_key)
        self.assertIn("nostr_relay_nip05_names", src,
                      "resolution no longer reads this node's own registry")
        # The DOCSTRING legitimately discusses profiles — it is where the reason not to trust them
        # is written down. Assert on the code, or this fails for explaining itself.
        fn = ast.parse(textwrap.dedent(src)).body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(getattr(fn.body[0], "value", None), ast.Constant)):
            fn.body = fn.body[1:]
        body = ast.unparse(fn).lower()
        for forbidden in ("kind0", "kind_0", "get_profile", "profof", "metadata"):
            self.assertNotIn(forbidden, body,
                             "resolution reached for a profile-supplied name (%r) — anyone can "
                             "write that claim about themselves" % forbidden)


if __name__ == "__main__":
    unittest.main()
