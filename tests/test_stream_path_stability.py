"""A viewer must not be moved between MediaMTX muxers mid-stream.

`<token>` and `<token>_clamped` are two separate muxers. Each has its own init segment and its own
media-sequence numbering, so switching a player from one to the other is a decoder reset — a stall, and on
some players a fatal error. The proxy used to re-decide which one to serve on EVERY request from a
5-second-cached liveness probe, so a single probe that failed to answer re-routed every viewer.

Measured on one nine-minute production stream (MediaMTX's own log, 2026-09-03):

    14:23:35  source  -> clamped   the clamp finishing its measurement — a guaranteed stall per stream
    14:27:23  clamped -> source    publisher reconnect
    14:27:47  source  -> clamped
    14:30:21  clamped -> source    nothing wrong; one probe did not answer in time
    14:31:41  source  -> clamped

Five swaps. The rule now is: decide ONCE PER PUBLISH SESSION, and only ever source -> clamped.

These tests drive the real resolver with a scripted MediaMTX, because the bug was never in the text of
the code — every individual answer it gave was defensible, and only the SEQUENCE was wrong. A test that
asks it once cannot see that, which is exactly why this shipped.
"""
import asyncio
import unittest
from unittest import mock

from app.routers import streams as R


SESS_A = "2026-09-03T14:23:07.1-06:00"
SESS_B = "2026-09-03T14:27:06.3-06:00"      # the same stream after an OBS reconnect


class _Fake:
    """A scripted MediaMTX + clamp.sh. `clamp_up` and `verdict` are read fresh on every probe, so a test
    can change what the world says between calls the way the real one changes underneath the proxy."""

    def __init__(self, session=SESS_A, clamp_up=False, verdict=("", "")):
        self.session = session
        self.clamp_up = clamp_up
        self.verdict = verdict
        self.probes = 0

    async def path_state(self, name):
        if self.session is None:
            return {}
        return {"ready": True, "readyTime": self.session}

    async def is_publishing(self, name):
        self.probes += 1
        if name.endswith(R.stream_service.CLAMP_SUFFIX):
            return self.clamp_up
        return True

    def clamp_decision(self, token):
        return self.verdict


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for d in (R._pins, R._sess_first, R._clamp_ready, R._hls_sessions, R._hls_locks):
            d.clear()
        self.fake = _Fake()
        self.patches = [
            mock.patch.object(R.stream_service, "clamp_enabled", lambda *a, **k: True),
            mock.patch.object(R.stream_service, "clamp_decision", self.fake.clamp_decision),
            mock.patch.object(R.stream_end_service, "path_state", self.fake.path_state),
            mock.patch.object(R.stream_end_service, "is_publishing", self.fake.is_publishing),
            # the per-request memo would otherwise hide a probe flap behind its own cache and make these
            # tests pass for the wrong reason
            mock.patch.object(R, "_CLAMP_TTL", 0.0),
            mock.patch.object(R, "_CLAMP_TTL_SETTLED", 0.0),
            mock.patch.object(R, "_STATE_TTL", 0.0),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    async def resolve(self, token="tok"):
        return await R._upstream_path(token, hold=0.0)


class TestOneDecisionPerSession(_Base):

    async def test_a_probe_that_flaps_does_not_move_a_viewer(self):
        """THE regression. The clamp is up, so the viewer is on the clamped path; then the control API
        stops answering for a moment. Before the pin that re-routed every viewer to a different muxer,
        and re-routed them back a minute later — twice in the nine minutes that were measured."""
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)
        for _ in range(5):                       # the probe goes dark, repeatedly
            self.fake.clamp_up = False
            self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX,
                             "a viewer was moved off the clamped muxer by a probe that did not answer")

    async def test_the_old_per_request_rule_really_did_flap(self):
        """Proof the test above can fail: run the rule this replaced and watch it swap.

        Without this, a pin that was accidentally unconditional would pass every assertion here and nobody
        would know the guard had stopped guarding anything.
        """
        async def old_rule(token):
            name = f"{token}{R.stream_service.CLAMP_SUFFIX}"
            return name if await self.fake.is_publishing(name) else token

        self.fake.clamp_up = True
        first = await old_rule("tok")
        self.fake.clamp_up = False
        self.assertNotEqual(first, await old_rule("tok"))

    async def test_a_reconnect_is_a_new_session_and_may_decide_again(self):
        """The pin is per PUBLISH, not forever: OBS reconnecting restarts the clamp, and the stream that
        comes back may legitimately be a different shape. MediaMTX stamps a fresh readyTime, which is what
        tells the two apart."""
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)
        self.fake.session = SESS_B                       # the publisher dropped and came back
        self.fake.clamp_up = False
        self.fake.verdict = ("source", SESS_B)
        self.assertEqual(await self.resolve(), "tok")

    async def test_a_verdict_from_the_previous_publish_is_ignored(self):
        """clamp.sh's decision file outlives the stream it describes. Read without checking the session it
        belongs to, a stale `source` would stand a stream down that is currently being clamped."""
        self.fake.session = SESS_B
        self.fake.verdict = ("source", SESS_A)           # left over from the previous broadcast
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)


class TestMonotonic(_Base):

    async def test_source_may_become_clamped_once(self):
        """The one transition worth a stall: clamp.sh re-measures a stood-down stream every ~45s, and a
        streamer whose OBS bitrate climbs mid-evening (916 kbit/s -> 10 Mbit/s was measured on this node)
        must get clamped rather than costing every viewer 10 Mbit/s for the rest of the night."""
        self.fake.verdict = ("source", SESS_A)
        self.assertEqual(await self.resolve(), "tok")
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)

    async def test_clamped_never_becomes_source(self):
        """The direction that is never right. Every flap that was measured in production was
        clamped -> source -> clamped, so this is the rule that removes all of them."""
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)
        self.fake.clamp_up = False
        self.fake.verdict = ("source", SESS_A)           # even clamp.sh changing its mind cannot move it
        for _ in range(3):
            self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)


class TestHolding(_Base):
    """The FIRST playlist request may wait a moment for an undecided session, so a viewer never starts on
    a path they are about to be moved off. Waiting is a slow response, never an error status: hls.js,
    native HLS on iOS Safari and third-party NIP-53 players all understand a slow answer and none of them
    understand a 503 as anything but a broken stream."""

    async def test_a_stood_down_stream_never_waits(self):
        """A phone under the ceiling will NEVER have a clamped path. Holding for it would put the full
        grace in front of every viewer of exactly the streams that are already cheap to serve."""
        self.fake.verdict = ("source", SESS_A)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self.assertEqual(await R._upstream_path("tok", hold=5.0), "tok")
        self.assertLess(loop.time() - t0, 0.5, "a stood-down stream was made to wait")

    async def test_a_pending_verdict_waits_and_then_serves_the_clamp(self):
        """`pending` is clamp.sh saying "a decision is coming". That is what makes the hold safe: without
        it the proxy cannot tell a clamp that is starting from a clamp.sh that is not running."""
        self.fake.verdict = ("pending", SESS_A)

        async def clamp_arrives():
            await asyncio.sleep(0.4)
            self.fake.clamp_up = True
            self.fake.verdict = ("clamped", SESS_A)

        task = asyncio.ensure_future(clamp_arrives())
        got = await R._upstream_path("tok", hold=5.0)
        await task
        self.assertEqual(got, "tok" + R.stream_service.CLAMP_SUFFIX)

    async def test_the_hold_always_ends(self):
        """A clamp that never arrives must degrade to a playable stream, not to a hung request. A stream
        that is genuinely playable right now beats a correct answer nobody is watching."""
        self.fake.verdict = ("pending", SESS_A)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self.assertEqual(await R._upstream_path("tok", hold=0.6), "tok")
        self.assertLess(loop.time() - t0, 3.0)

    async def test_giving_up_settles_so_the_next_refresh_does_not_wait_again(self):
        """A player polls its playlist every couple of seconds. If the request that gave up waiting does
        not SETTLE the session, the next refresh waits the whole hold again — a stall in the middle of a
        stream that was already playing, which is worse than the swap the hold exists to prevent.

        This is the shape a first-ever WebRTC stream has: its measurement is longer than any hold worth
        making a viewer sit through, so giving up is the normal path, not the exceptional one.
        """
        self.fake.verdict = ("pending", SESS_A)          # a clamp that takes longer than the hold
        loop = asyncio.get_running_loop()
        self.assertEqual(await R._upstream_path("tok", hold=0.4), "tok")
        t0 = loop.time()
        for _ in range(3):                                # the playlist refreshes that follow
            self.assertEqual(await R._upstream_path("tok", hold=5.0), "tok")
        self.assertLess(loop.time() - t0, 1.0, "a playing stream was stalled by a second hold")

    async def test_settling_on_the_source_still_allows_the_clamp_to_arrive(self):
        """Giving up must not be permanent: the clamp is still coming, and it is still worth one swap."""
        self.fake.verdict = ("pending", SESS_A)
        self.assertEqual(await R._upstream_path("tok", hold=0.3), "tok")
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)

    async def test_clamp_sh_never_speaking_settles_on_the_source(self):
        """MediaMTX can be running a frozen config (an orphan that survived a restart — see _kill_stale),
        in which case runOnReady never fires and no verdict is ever written. That must resolve to a
        watchable stream after a short grace, not hold every viewer for ever."""
        with mock.patch.object(R, "_VERDICT_GRACE", 0.0), mock.patch.object(R, "_STATE_TTL", 0.0):
            # The first sighting of a session starts its grace and cannot already have outlived it, so the
            # pin lands on the next request — which for a real viewer is the very next segment.
            self.assertEqual(await R._upstream_path("tok", hold=0.0), "tok")
            self.assertEqual(await R._upstream_path("tok", hold=0.0), "tok")
        self.assertIn("tok", R._pins)


class TestProbeCost(_Base):
    """A popular stream re-fetches a playlist and a segment every couple of seconds, per viewer. Every
    unmemoised probe in this path is a control-API round trip multiplied by that."""

    async def test_a_pinned_stream_does_not_probe_on_every_segment(self):
        self.fake.clamp_up = True
        self.assertEqual(await self.resolve(), "tok" + R.stream_service.CLAMP_SUFFIX)
        before = self.fake.probes
        for _ in range(20):
            await self.resolve()
        self.assertLessEqual(self.fake.probes - before, 2,
                             "the clamp probe is running per request despite the pin")


class TestSegmentsNeverWait(_Base):
    """A segment is fetched against the path its playlist already named, so holding one delays playback to
    re-answer a settled question."""

    async def test_only_the_playlist_is_given_a_hold(self):
        seen = []

        async def spy(token, hold=0.0):
            seen.append((token, hold))
            return token

        with mock.patch.object(R, "_upstream_path", spy):
            for path in ("index.m3u8", "video1_stream.m3u8", "seg12.mp4", "init.mp4"):
                await R._upstream_path("tok", hold=R._HOLD_MAX if path.endswith(".m3u8") else 0.0)
        self.assertEqual([h > 0 for _, h in seen], [True, True, False, False])


class TestHlsSessionSingleFlight(unittest.IsolatedAsyncioTestCase):
    """Priming opens a NEW MediaMTX HLS session and abandons the old one, so every needless prime is
    churn. Two things used to cause it: a 20s timer, and no single-flight — concurrent viewers each
    started their own prime, which is how three sessions appeared in the same second for one stream."""

    def setUp(self):
        R._hls_sessions.clear()
        R._hls_locks.clear()

    async def test_concurrent_callers_prime_exactly_once(self):
        calls = []

        async def fake_prime(port, token):
            calls.append(token)
            await asyncio.sleep(0.05)
            R._hls_sessions[token] = ("cookieCheck=1; hlsSession=x", R.time.monotonic())
            return "cookieCheck=1; hlsSession=x"

        with mock.patch.object(R, "_prime_hls_session", fake_prime):
            got = await asyncio.gather(*[R._hls_session_cookie("8888", "tok") for _ in range(8)])
        self.assertEqual(len(calls), 1, f"primed {len(calls)} sessions for one stream")
        self.assertEqual(len(set(got)), 1)

    async def test_a_forced_refresh_that_already_happened_is_not_repeated(self):
        """Every viewer's request 401s at once when MediaMTX rotates a session. The first forced refresh
        fixes it for all of them; the rest must adopt that session rather than stacking one prime each."""
        R._hls_sessions["tok"] = ("stale", R.time.monotonic())
        calls = []

        async def fake_prime(port, token):
            calls.append(token)
            await asyncio.sleep(0.05)
            R._hls_sessions[token] = ("fresh", R.time.monotonic())
            return "fresh"

        with mock.patch.object(R, "_prime_hls_session", fake_prime):
            got = await asyncio.gather(*[R._hls_session_cookie("8888", "tok", force=True)
                                         for _ in range(6)])
        self.assertEqual(len(calls), 1, f"{len(calls)} refreshes for one rotation")
        self.assertEqual(set(got), {"fresh"})


if __name__ == "__main__":
    unittest.main()
