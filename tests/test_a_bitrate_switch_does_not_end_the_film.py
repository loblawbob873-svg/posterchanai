"""Reporting Stopped must not make every later request for that play session 404.

A Jellyfin client changes quality by reporting `Sessions/Playing/Stopped` and immediately asking for
the new rendition — with the SAME PlaySessionId. `stopped()` did `_plays.pop(play_id)`, so every
request after that report raised "Playback session expired; reopen the item", which a TV shows as
"Error During Playback" in the middle of a film.

Measured on a real play, all inside one second:

    GET  Videos/<id>/master.m3u8   200      <- still fine
    POST Sessions/Playing/Stopped  204      <- the record was popped here
    GET  Videos/<id>/360p.m3u8     404
    GET  Videos/<id>/master.m3u8   404      <- had answered 200 a second earlier

`master.m3u8` skips the profile check, so its own 200 -> 404 is what proves the RECORD went rather
than the rendition.

Keeping the record is safe: the `ticket` in its url is an HMAC over (library, item, pubkey, expires)
which media_center verifies BY SIGNATURE (`hmac.compare_digest` against `sign_ticket`), so it stays
valid until it expires whether or not a session is counted; `/sessions/stop` frees the
concurrent-stream slot, which is the only thing a stop actually releases.
"""
import time
import unittest
from unittest import mock

from app.routers import jellyfin


class _Auth:
    token = "tok"


def _record():
    return {
        "token": jellyfin.digest(_Auth.token),
        "item": "879a7455759d34030e7df3b8fd9ec95e",
        "url": "/api/media-center/1/stream/9?viewer=pk&expires=99999999999&ticket=" + ("a" * 64),
        "seen": time.monotonic(),
        "profiles": ["480p", "360p"],
        "library_id": 1,
        "native_id": 9,
    }


class BitrateSwitch(unittest.TestCase):
    def setUp(self):
        jellyfin._plays.clear()
        self.play_id = "0594b824cc3d86b4286d264ed6a62f7c"
        jellyfin._plays[self.play_id] = _record()

    def _stop(self):
        """Run the real handler with its network call and progress write stubbed out."""
        import asyncio
        with mock.patch.object(jellyfin, "media_call", new=mock.AsyncMock(return_value={})) as call, \
             mock.patch.object(jellyfin, "persist_progress", new=mock.AsyncMock()):
            asyncio.run(jellyfin.stopped(
                request=mock.Mock(), body={"PlaySessionId": self.play_id},
                auth=_Auth(), db=mock.Mock()))
        return call

    def test_the_session_survives_a_stop_report(self):
        """The regression."""
        self._stop()
        self.assertIn(self.play_id, jellyfin._plays,
                      "the play record was dropped — the next request 404s mid-film")

    def test_the_playlist_still_resolves_afterwards(self):
        """This is the exact call that answered 200 and then 404 one second later."""
        self._stop()
        rec = jellyfin.play_record(_Auth(), self.play_id, "879a7455759d34030e7df3b8fd9ec95e")
        self.assertTrue(rec, "play_record refuses the session the client is still using")
        self.assertIn("360p", rec["profiles"], "the new rendition is no longer offered")

    def test_the_stream_slot_is_still_given_back(self):
        """Keeping the record must not leak the concurrent-stream slot a stop exists to free."""
        call = self._stop()
        paths = [c.args[3] for c in call.await_args_list if len(c.args) > 3]
        self.assertIn("/sessions/stop", paths,
                      "the media-center session was not released, so the slot leaks")

    def test_a_stopped_session_is_marked_as_such(self):
        self._stop()
        self.assertTrue(jellyfin._plays[self.play_id].get("stopped"),
                        "nothing records that the client reported a stop")

    def test_it_is_still_bounded(self):
        """Not popping must not mean growing without limit."""
        import inspect
        src = inspect.getsource(jellyfin.play_record)
        self.assertIn("900", src, "the staleness bound on a play record is gone")
        self.assertIn("_plays.popitem(last=False)", inspect.getsource(jellyfin.playback_info),
                      "the 256-entry cap is gone")


if __name__ == "__main__":
    unittest.main()
