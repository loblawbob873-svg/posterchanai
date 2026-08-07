"""A stream that ENDED is 404, not 502 — and the difference is a retry storm.

MediaMTX only generates HLS while a publisher is connected, so the overwhelmingly common reason the
proxy cannot prime a session is that the stream is over. Reporting that as `502 Bad Gateway` tells
every player the gateway is broken and to keep trying, and they do: the journal for one ended stream
carried ~200 failed manifest fetches per five minutes, for hours, plus a client firing twelve
identical `vods/by-token` requests inside a single second. That load lands on the same box that has
to encode the next stream, which is the machine the viewers are complaining about.

A genuinely unreachable MediaMTX keeps its 502, because that one IS worth retrying — so the two
cases have to be told apart rather than collapsed.
"""
import asyncio

import pytest

from app.routers import streams as streams_router


def _status(is_publishing):
    """Run the helper with `is_publishing` stubbed, and give back the status code it chose."""
    async def _fake(name):
        return is_publishing() if callable(is_publishing) else is_publishing

    orig = streams_router.stream_end_service.is_publishing
    streams_router.stream_end_service.is_publishing = _fake
    try:
        return asyncio.new_event_loop().run_until_complete(
            streams_router._hls_gone_or_502("tok")).status_code
    finally:
        streams_router.stream_end_service.is_publishing = orig


def test_an_ended_stream_is_404_so_players_stop_retrying():
    assert _status(False) == 404, (
        "a finished stream answers 502, which every player reads as 'retry' — that is the storm "
        "that hammered the box for hours after the stream was over")


def test_a_live_stream_that_cannot_be_proxied_is_still_502():
    assert _status(True) == 502, (
        "the stream IS publishing, so the proxy failing is a real gateway fault and retrying is the "
        "right thing for a player to do; 404 would make a recoverable blip look permanent")


def test_an_unanswerable_probe_fails_to_gone():
    """If MediaMTX cannot even be asked, prefer the answer that does NOT start a retry loop."""
    def _boom():
        raise RuntimeError("mediamtx api unreachable")

    assert _status(_boom) == 404


@pytest.mark.parametrize("src", ["502 Bad Gateway", "Response(status_code=502)"])
def test_the_proxy_no_longer_returns_a_bare_502_for_a_missing_session(src):
    """The three returns inside the HLS proxy must all route through the helper.

    They were three separate `return Response(status_code=502)` lines; leaving any one of them turns
    that path back into a retry loop, and nothing about it fails loudly.
    """
    import inspect
    body = inspect.getsource(streams_router.stream_hls_proxy)
    assert "Response(status_code=502)" not in body, \
        "a bare 502 is back in the HLS proxy — an ended stream will restart the retry storm"
    assert body.count("_hls_gone_or_502(src)") == 3, \
        "not every failure path in the proxy distinguishes 'ended' from 'broken'"
