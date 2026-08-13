"""A YouTube link in News is summarized from its TRANSCRIPT, not by scraping the watch page.

`/api/news/summarize` fetched every URL as HTML through the news proxy, which is Tor. YouTube
answers a Tor exit with a redirect to google.com/sorry — the CAPTCHA wall — so `raise_for_status`
threw and the user was handed the raw text of a 429 against a `google.com/sorry` URL. Measured in
the log: two attempts, both 429, both surfaced verbatim. It reads like the summarizer is broken,
when the page was never going to be readable from there: CLAUDE.md already records that Google
answers Tor exits with "too many requests"/CAPTCHA, which is why SearXNG does not route through it
either.

The app already had the right answer and AI Chat has been using it the whole time (`yt` →
summarize_youtube → get_transcript). This makes News ask the same question.

  transcript-is-used        a YouTube URL is summarized and NO page fetch is made — the request that
                            produced the 429 is the one not sent
  ordinary-articles-unchanged   a normal URL still goes through the proxy fetch, or this "fix" has
                            quietly disabled news summarization
  no-transcript-says-so     a video with subtitles disabled gets a sentence, not an HTTP error —
                            "no summary" for a blocked fetch and "no summary" for a video with no
                            captions are the same words and different bugs
  no-name-error-on-the-new-path   there is no `response` on the transcript branch, and an unguarded
                            read of it is a NameError the outer except turns into
                            "Error: name 'response' is not defined" — worse than the 429 it replaced
"""
import asyncio
from unittest import mock

import pytest

from app.routers import news
from app.services import youtube_service as ys


def _run(*, is_yt, transcript, html=None):
    """Drive the real handler with the network and the model stubbed. Returns (result, fetches)."""
    html = html if html is not None else "<html><body>" + ("article text " * 60) + "</body></html>"
    fetched = {"n": 0}

    class _Resp:
        status_code = 200
        text = html

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            fetched["n"] += 1
            return _Resp()

    svc = mock.Mock(chat_completion=mock.AsyncMock(
        return_value={"choices": [{"message": {"content": "SUMMARY"}}]}))

    with mock.patch.object(ys, "is_youtube_url", lambda u: is_yt), \
         mock.patch.object(ys, "extract_video_id", lambda u: "vid"), \
         mock.patch.object(ys, "get_transcript", lambda v: transcript), \
         mock.patch.object(news, "require_proxy", lambda *a, **k: "socks5://127.0.0.1:9050"), \
         mock.patch.object(news, "prepare_vram_for_llm", lambda db: None), \
         mock.patch("httpx.AsyncClient", _Client), \
         mock.patch.object(news, "get_inference_service", lambda db: svc):
        out = asyncio.run(news.summarize_article("https://example/x", db=None, current_user=None))
    return out, fetched["n"]


def test_transcript_is_used_and_the_page_is_never_fetched():
    out, fetches = _run(is_yt=True, transcript="words " * 50)
    assert out["summary"] == "SUMMARY", out
    assert fetches == 0, (
        "the watch page was fetched anyway — that request is the one that gets a 429 from "
        "google.com/sorry through the Tor news proxy")


def test_ordinary_articles_still_go_through_the_proxy_fetch():
    out, fetches = _run(is_yt=False, transcript=None)
    assert out["summary"] == "SUMMARY", out
    assert fetches == 1, "news summarization stopped fetching ordinary articles"


def test_a_video_with_no_transcript_says_so():
    out, fetches = _run(is_yt=True, transcript=None)
    assert fetches == 0
    assert "transcript" in out["summary"].lower(), out
    assert "429" not in out["summary"] and "Error:" not in out["summary"], (
        "a video without captions must not surface an HTTP error — that is the message that made "
        "this look like a broken summarizer")


def test_the_transcript_branch_does_not_touch_response():
    """The NameError this would otherwise be. `response` only exists on the fetch branch."""
    out, _ = _run(is_yt=True, transcript="words " * 50)
    assert "not defined" not in out["summary"], out
    assert "response" not in out["summary"], out
