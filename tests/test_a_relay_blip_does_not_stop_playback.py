"""A video segment must not depend on opening a websocket.

Run: venv-unified/bin/python -m unittest tests.test_a_relay_blip_does_not_stop_playback

Reported as "jellyfin stopped playing twice". Nothing had restarted, nothing was wrong with the
media, the token or the session. From the journal, twice, mid-film:

    app/routers/jellyfin.py", line 115, in authenticate
    app/services/media_center.py", line 467, in read
    app/services/nostr_store.py", line 217, in _ws_query
    TimeoutError: timed out during opening handshake
    GET /jellyfin/Videos/<id>/480p-188.ts  ->  500 Internal Server Error

`authenticate` reads the account record to find the session matching the token, and
`nostr_store._ws_query` opens its OWN websocket per call — so the read sat in front of every
request, including one `.ts` segment every two seconds. An unhandled exception in an auth
dependency is a 500, and a 500 on a segment is the end of playback.

Two rules, and the second is what makes the first safe:

  * the record is CACHED, so a stream is not a websocket per segment; and
  * "could not ask" is never a denial — a record that was read successfully keeps answering while
    the relay is unreachable, bounded by ACCOUNT_STALE_OK.

What is cached is WHICH SESSIONS EXIST. Whether the account may watch is re-decided per request
against the database (`media_allowed`, the user lookup, `require_user`), so this can never widen
access — only avoid ending a session over a socket timeout.
"""
import asyncio
import time
import unittest

from app.services import media_center as media


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class ARelayBlipDoesNotStopPlayback(unittest.TestCase):

    def setUp(self):
        media._account_cache.clear()
        self.reads = []
        self.record = {'pubkey': 'a' * 64, 'sessions': [{'id': 's1', 'hash': 'h', 'expires': 1 << 40}]}
        self.fail_with = None

        async def read(key):
            self.reads.append(key)
            if self.fail_with:
                raise self.fail_with
            return self.record
        self._real_read = media.read
        media.read = read

    def tearDown(self):
        media.read = self._real_read
        media._account_cache.clear()

    def test_a_stream_is_not_a_websocket_per_segment(self):
        """The shape of the bug: a player asks every two seconds."""
        async def go():
            for _ in range(30):                       # a minute of playback
                await media.read_account('account:x')
        _run(go())
        self.assertEqual(len(self.reads), 1,
                         "the account record was read %d times for one minute of playback — every "
                         "one of those is a fresh websocket to the relay in front of a video "
                         "segment" % len(self.reads))

    def test_a_relay_that_stops_answering_does_not_end_the_session(self):
        """The failure that was actually measured: one timed-out handshake, one 500, one stopped
        film. A record we have already read must keep answering."""
        got = _run(media.read_account('account:x'))
        self.assertEqual(got, self.record)
        self.fail_with = TimeoutError('timed out during opening handshake')
        media._account_cache['account:x'] = (self.record, time.time() - media.ACCOUNT_FRESH - 1)
        self.assertEqual(_run(media.read_account('account:x')), self.record,
                         "a websocket handshake timeout ended a session that was valid — this is "
                         "the 500 on a .ts segment, which is the end of playback")

    def test_could_not_ask_is_bounded_not_forever(self):
        """The trade is stated and it has an edge: a session revoked during an outage must not stay
        usable indefinitely."""
        _run(media.read_account('account:x'))
        self.fail_with = TimeoutError('timed out during opening handshake')
        media._account_cache['account:x'] = (self.record, time.time() - media.ACCOUNT_STALE_OK - 1)
        with self.assertRaises(TimeoutError):
            _run(media.read_account('account:x'))

    def test_a_cold_cache_still_reports_the_failure(self):
        """Nothing is invented. With no copy to fall back on, the caller is told."""
        self.fail_with = TimeoutError('timed out during opening handshake')
        with self.assertRaises(TimeoutError):
            _run(media.read_account('account:never-read'))

    def test_a_logout_takes_effect_at_once(self):
        """A cache in front of a session list is a revocation delay unless the writers drop it."""
        _run(media.read_account('account:x'))
        self.record = {'pubkey': 'a' * 64, 'sessions': []}
        self.assertTrue(_run(media.read_account('account:x'))['sessions'],
                        "the fixture is not exercising the cache at all")
        media.forget_account('account:x')
        self.assertEqual(_run(media.read_account('account:x'))['sessions'], [],
                         "a logout/revoke did not take effect — forget_account must drop the copy")

    def test_a_missing_record_is_never_cached(self):
        """An account created a second ago has to be able to log in."""
        self.record = None
        _run(media.read_account('account:new'))
        self.record = {'pubkey': 'b' * 64, 'sessions': []}
        self.assertIsNotNone(_run(media.read_account('account:new')),
                             "a negative answer was cached, so a fresh account cannot sign in "
                             "until the window expires")

    def test_the_cache_is_bounded(self):
        for i in range(media._ACCOUNT_MAX + 50):
            _run(media.read_account('account:%d' % i))
        self.assertLessEqual(len(media._account_cache), media._ACCOUNT_MAX)


class TheAuthDependencyAnswersInAStatus(unittest.TestCase):
    """A 500 tells a player nothing and is indistinguishable from a broken library. Whatever the
    relay does, `authenticate` has to end in an HTTP status."""

    def test_an_unreachable_relay_is_a_503_not_an_unhandled_exception(self):
        import inspect
        from app.routers import jellyfin as jf
        src = inspect.getsource(jf.authenticate)
        self.assertIn("read_account", src,
                      "authenticate is reading the account record straight through again — that is "
                      "a websocket per video segment")
        self.assertIn("503", src,
                      "a failed read escapes authenticate unhandled, which answers a .ts segment "
                      "with a 500 and ends playback")


if __name__ == "__main__":
    unittest.main()
