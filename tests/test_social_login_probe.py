"""Signing in must never MINT an account because a lookup failed.

Run: venv-unified/bin/python -m unittest tests.test_social_login_probe

_find_pleroma_user answers "is this fediverse account already linked here?". Accounts linked before
pleroma_acct existed carry no handle, so it probes their stored tokens to find out. The caller creates
a brand-new account on "no match" — so a probe that ERRORS must not be reported as one. It was: every
exception became `acct = ""`, i.e. "not this person", and one dropped request would hand an existing
user a second, empty identity. That is exactly how the Google flow produced three strays in eight
minutes, and the same failure/absence conflation that silently deleted mentions in the bridge.

A 401/403 IS an answer — that token is dead, so it genuinely isn't a match. Anything else (429 from
probing 25 tokens at once, a 5xx, a reset) is not an answer and has to fail closed.
"""
import unittest
from unittest import mock

import httpx

from app.routers import social_login as S


def _user(uid, token="tok", acct=None):
    u = mock.Mock(id=uid, pleroma_access_token=token, pleroma_acct=acct,
                  pleroma_instance_url="https://inst")
    return u


def _status_error(code):
    req = httpx.Request("GET", "https://inst/api/v1/accounts/verify_credentials")
    return httpx.HTTPStatusError("boom", request=req,
                                 response=httpx.Response(code, request=req))


class FindPleromaUser(unittest.IsolatedAsyncioTestCase):
    def _db(self, direct_hit=None, unlabelled=()):
        db = mock.Mock()
        q = mock.Mock()
        q.filter.return_value = q
        q.first.return_value = direct_hit
        q.limit.return_value.all.return_value = list(unlabelled)
        db.query.return_value = q
        return db

    async def _find(self, db, verify):
        with mock.patch("app.services.pleroma_service.verify_credentials", verify):
            return await S._find_pleroma_user(db, "https://inst", "alice@inst")

    async def test_direct_handle_match_never_probes(self):
        hit = _user(1, acct="alice@inst")
        probe = mock.AsyncMock(side_effect=AssertionError("should not probe"))
        self.assertIs(await self._find(self._db(direct_hit=hit), probe), hit)

    async def test_probe_finds_and_backfills_the_owner(self):
        u = _user(2)
        db = self._db(unlabelled=[u])
        got = await self._find(db, mock.AsyncMock(return_value={"acct": "alice"}))
        self.assertIs(got, u)
        self.assertEqual(u.pleroma_acct, "alice@inst")   # never probed again

    async def test_revoked_token_is_a_real_answer_not_a_match(self):
        """401 means that token is dead — a definite "not this person", so signup may proceed."""
        db = self._db(unlabelled=[_user(3)])
        got = await self._find(db, mock.AsyncMock(side_effect=_status_error(401)))
        self.assertIsNone(got)

    async def test_transient_failure_refuses_to_answer(self):
        """The caller MINTS on None, so a failed probe must raise instead of returning it."""
        for exc in (_status_error(429), _status_error(503), httpx.ConnectError("reset"),
                    httpx.ReadTimeout("slow")):
            with self.subTest(exc=type(exc).__name__):
                db = self._db(unlabelled=[_user(4)])
                with self.assertRaises(S._ProbeUnavailable):
                    await self._find(db, mock.AsyncMock(side_effect=exc))

    async def test_unprobed_accounts_past_the_cap_are_unknown_not_absent(self):
        """The cap bounds work per login, but an account we never asked about isn't an account that
        doesn't exist — the owner may be one of them, so signup must not proceed on that silence.
        Each probe backfills permanently, so a retry drains the backlog and the condition clears."""
        many = [_user(100 + i) for i in range(S._ACCT_BACKFILL_LIMIT + 1)]
        db = self._db(unlabelled=many)
        with self.assertRaises(S._ProbeUnavailable):
            await self._find(db, mock.AsyncMock(return_value={"acct": "someone-else"}))

    async def test_a_match_wins_even_if_another_probe_failed(self):
        """One dead token among several must not block a user whose own probe answered."""
        ok, bad = _user(5), _user(6)
        async def verify(_inst, token):
            if token == "bad":
                raise httpx.ConnectError("reset")
            return {"acct": "alice"}
        bad.pleroma_access_token = "bad"
        db = self._db(unlabelled=[bad, ok])
        with mock.patch("app.services.pleroma_service.verify_credentials", verify):
            got = await S._find_pleroma_user(db, "https://inst", "alice@inst")
        self.assertIs(got, ok)

    async def test_resolved_handles_are_kept_when_another_probe_fails(self):
        """The backfill is the whole point of probing — a sibling failure must not discard it."""
        other, bad = _user(7), _user(8)
        bad.pleroma_access_token = "bad"
        async def verify(_inst, token):
            if token == "bad":
                raise httpx.ConnectError("reset")
            return {"acct": "bob"}
        db = self._db(unlabelled=[bad, other])
        with mock.patch("app.services.pleroma_service.verify_credentials", verify):
            with self.assertRaises(S._ProbeUnavailable):     # "alice" still unresolved
                await S._find_pleroma_user(db, "https://inst", "alice@inst")
        self.assertEqual(other.pleroma_acct, "bob@inst")
        self.assertTrue(db.commit.called)


if __name__ == "__main__":
    unittest.main()
