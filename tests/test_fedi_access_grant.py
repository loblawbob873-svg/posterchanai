"""A fediverse sign-in grants AI + Blossom — but a REVOCATION has to survive the next sign-in.

Run: venv-unified/bin/python -m unittest tests.test_fedi_access_grant

Holding an account on an instance we allow sign-in from is the identity check `can_ai` stands in for,
so unlike a bare Nostr signup (anyone can mint a keypair) a fedi sign-in hands out AI + Blossom. It
runs on EVERY sign-in, not just the first, so people who linked earlier aren't left locked out.

That is exactly what makes `access_revoked` load-bearing: without it the grant would undo an admin's
revocation the next time the user logged in, so the one lever for dealing with abuse would silently
last only until their next visit. These tests pin that, and pin that both revoke paths (the admin
capability form and the client's AI toggle) actually set the marker.
"""
import unittest
from unittest import mock

from app.routers.social_login import apply_fedi_access


class FakeUser:
    def __init__(self, can_ai=False, can_blossom=False, access_revoked=False):
        self.can_ai = can_ai
        self.can_blossom = can_blossom
        self.access_revoked = access_revoked
        self.username = "someone"


class TestApplyFediAccess(unittest.TestCase):
    def test_grants_both_to_a_fresh_account(self):
        u = FakeUser()
        self.assertTrue(apply_fedi_access(u))
        self.assertTrue(u.can_ai and u.can_blossom)

    def test_fills_in_a_missing_half(self):
        """Everyone linked before this had can_ai but not can_blossom — the common real case."""
        u = FakeUser(can_ai=True, can_blossom=False)
        self.assertTrue(apply_fedi_access(u))
        self.assertTrue(u.can_blossom)

    def test_no_change_reports_false(self):
        u = FakeUser(can_ai=True, can_blossom=True)
        self.assertFalse(apply_fedi_access(u))

    def test_revoked_access_is_not_handed_back(self):
        """The point of the marker: signing in again must not undo an admin's revocation."""
        u = FakeUser(can_ai=False, can_blossom=False, access_revoked=True)
        self.assertFalse(apply_fedi_access(u))
        self.assertFalse(u.can_ai)
        self.assertFalse(u.can_blossom)

    def test_revoked_beats_a_partial_grant(self):
        u = FakeUser(can_ai=True, can_blossom=False, access_revoked=True)
        self.assertFalse(apply_fedi_access(u))
        self.assertFalse(u.can_blossom)

    def test_missing_column_is_treated_as_not_revoked(self):
        """The column arrives by startup migration; a row read before it exists must still grant."""
        class Old:
            can_ai = False
            can_blossom = False
        u = Old()
        self.assertTrue(apply_fedi_access(u))


class TestRevokePathsSetTheMarker(unittest.TestCase):
    """Both revoke routes must mark, or the grant above quietly reverses them."""

    def test_admin_capability_form_marks_and_clears(self):
        import inspect
        from app.routers import admin
        src = inspect.getsource(admin.update_user_capabilities)
        self.assertIn("access_revoked = True", src)
        self.assertIn("access_revoked = False", src)

    def test_client_ai_toggle_marks(self):
        import inspect
        from app.routers import client
        src = inspect.getsource(client.ai_access)
        self.assertIn("access_revoked", src)


if __name__ == "__main__":
    unittest.main()
