"""A Blossom listing can be asked for a SLICE, because the whole thing is 9.7 MB.

Run: venv-unified/bin/python -m unittest tests.test_blossom_list_can_be_bounded

Measured on this deployment: one pubkey owns 37,400 blobs, and `GET /blossom/list/<pk>` answers in
1.1s with 9,719,856 bytes of JSON. The server is not slow — the client has to download and parse all
of it before it can draw one thumbnail, which is what "trying to attach files from Blossom, and
circle ....never loading" was.

`since`/`until` are BUD-02's own filters; `limit` is an extension that takes the NEWEST n. The rule
that matters most is the LAST test: with no parameters the response must be byte-identical to what
every existing client and the protocol already get.
"""
import asyncio
import json
import unittest
from unittest import mock

from app.routers import blossom as blossom_router


def _blob(sha: str, created_at: int):
    return mock.Mock(sha256=sha, size=10, mime="image/png", created_at=created_at, keep=False)


# Deliberately NOT in timestamp order: the route must not depend on the query's ordering, and a cut
# that only works on pre-sorted input would pass on a fixture and drop the newest blobs in production.
BLOBS = [_blob(f"{i:064x}", 1000 + i) for i in (3, 7, 1, 9, 5, 2, 8, 4, 6, 0)]


def _call(**params):
    async def go():
        with mock.patch.object(blossom_router.blossom_service, "is_enabled", return_value=True), \
             mock.patch.object(blossom_router.blossom_service, "list_for_pubkey", return_value=list(BLOBS)), \
             mock.patch.object(blossom_router.blossom_service, "names_for_pubkey", return_value={}), \
             mock.patch.object(blossom_router, "_base_url", return_value="https://m.example/blossom"):
            return await blossom_router.list_blobs("f" * 64, mock.Mock(), db=mock.Mock(), **params)
    resp = asyncio.run(go())
    return json.loads(bytes(resp.body).decode())


class BoundedListing(unittest.TestCase):
    def test_limit_takes_the_newest(self):
        rows = _call(limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(r["uploaded"] for r in rows), [1007, 1008, 1009])

    def test_limit_larger_than_the_drive_returns_everything(self):
        self.assertEqual(len(_call(limit=5000)), len(BLOBS))

    def test_limit_zero_is_honoured_rather_than_read_as_no_limit(self):
        # `if limit:` would treat 0 as absent and dump the whole 9.7 MB.
        self.assertEqual(_call(limit=0), [])

    def test_since_and_until_are_inclusive_bounds(self):
        self.assertEqual(sorted(r["uploaded"] for r in _call(since=1007)), [1007, 1008, 1009])
        self.assertEqual(sorted(r["uploaded"] for r in _call(until=1002)), [1000, 1001, 1002])
        self.assertEqual(sorted(r["uploaded"] for r in _call(since=1003, until=1005)),
                         [1003, 1004, 1005])

    def test_a_bounded_response_keeps_the_route_s_own_ordering(self):
        # The cut is taken newest-first, but the response must come back in the order the listing
        # query produced — a client that does not sort must not see its drive reshuffled.
        rows = _call(limit=5)
        kept = [r["uploaded"] for r in rows]
        self.assertEqual(kept, [b.created_at for b in BLOBS if b.created_at in set(kept)])

    def test_no_parameters_is_byte_identical_to_the_unbounded_listing(self):
        """The protocol route, unchanged. This is the one that must never break."""
        self.assertEqual(_call(), _call(since=None, until=None, limit=None))
        self.assertEqual([r["uploaded"] for r in _call()], [b.created_at for b in BLOBS])
        self.assertEqual(len(_call()), len(BLOBS))


class TheCutIsAppliedBeforeTheExpensiveHalf(unittest.TestCase):
    def test_descriptors_are_built_only_for_the_rows_that_survive(self):
        """descriptor() does per-blob string work; building 37,400 of them to return 2,000 is the
        cost this whole change exists to avoid."""
        real = blossom_router.blossom_service.descriptor
        calls = []

        def counting(blob, base, name=""):
            calls.append(blob.sha256)
            return real(blob, base, name=name)

        async def go():
            with mock.patch.object(blossom_router.blossom_service, "is_enabled", return_value=True), \
                 mock.patch.object(blossom_router.blossom_service, "list_for_pubkey", return_value=list(BLOBS)), \
                 mock.patch.object(blossom_router.blossom_service, "names_for_pubkey", return_value={}), \
                 mock.patch.object(blossom_router.blossom_service, "descriptor", side_effect=counting), \
                 mock.patch.object(blossom_router, "_base_url", return_value="https://m.example/blossom"):
                await blossom_router.list_blobs("f" * 64, mock.Mock(), db=mock.Mock(), limit=3)
        asyncio.run(go())
        self.assertEqual(len(calls), 3, "the listing was fully rendered and then thrown away")


if __name__ == "__main__":
    unittest.main()
