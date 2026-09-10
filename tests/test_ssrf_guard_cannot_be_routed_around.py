"""A GUARD ANY BRANCH ABOVE IT CAN SKIP IS NOT A GUARD.

`fetch_url_content` carries an SSRF check with the comment "SSRF protection: validate URL before
fetching" — and it sat BELOW the YouTube interception. `youtube_service.extract_video_id` matches
with an UNANCHORED `re.search`, so a url merely had to CONTAIN `youtube.com/watch?v=<11 chars>` to be
routed past the guard entirely into `_fetch_youtube_content`, which fetched with
`follow_redirects=True` and no validation at all.

MEASURED before the fix — each of these yields a video id AND is refused by `is_safe_url`, i.e. the
guard would have stopped them and never ran:

    http://169.254.169.254/latest/meta-data/?x=youtube.com/watch?v=dQw4w9WgXcQ
    http://127.0.0.1:5432/?youtube.com/watch?v=dQw4w9WgXcQ
    http://192.168.0.85:3051/api/#youtu.be/dQw4w9WgXcQ

Every caller reaches this — telegram, the web UI, the fediverse bots, the summarize/post commands —
so a pasted link was enough, and the fetched page's <title> comes back to the caller and to the
model. The cloud metadata endpoint is the classic target and this repo has already had one 302 reach
169.254.169.254 once before.
"""
import inspect
import re

import pytest

from app.services.search_service import SearchService
from app.services.youtube_service import extract_video_id
from app.services.rss_service import is_safe_host

BYPASSES = [
    "http://169.254.169.254/latest/meta-data/?x=youtube.com/watch?v=dQw4w9WgXcQ",
    "http://127.0.0.1:5432/?youtube.com/watch?v=dQw4w9WgXcQ",
    "http://192.168.0.85:3051/api/#youtu.be/dQw4w9WgXcQ",
    "http://[::1]/?youtube.com/embed/dQw4w9WgXcQ",
]


def _code(fn):
    """Source with comments stripped — this test is about ORDER, and the comments below name the
    very calls being ordered."""
    return "\n".join(l for l in inspect.getsource(fn).splitlines()
                     if not l.strip().startswith("#"))


@pytest.mark.parametrize("url", BYPASSES)
def test_these_urls_still_look_like_youtube_and_are_still_unsafe(url):
    """The premise. If either half stops being true the test is measuring nothing."""
    assert extract_video_id(url), "no longer routed down the YouTube path — premise gone"
    assert not is_safe_host(url), "the SSRF guard no longer objects to this — premise gone"


def test_the_ssrf_check_runs_before_the_youtube_branch():
    code = _code(SearchService.fetch_url_content)
    assert code.index("is_safe_url(url)") < code.index("_yt.extract_video_id(url)"), (
        "the YouTube interception is above the SSRF guard again — any url containing "
        "'youtube.com/watch?v=<11 chars>' is fetched unvalidated")


def test_nothing_is_fetched_before_the_guard():
    """Not just the YouTube branch: no network call of any kind may precede the check."""
    code = _code(SearchService.fetch_url_content)
    guard = code.index("is_safe_url(url)")
    before = code[:guard]
    for call in ("client.get", "client.post", "httpx.", "_fetch_youtube_content"):
        assert call not in before, f"{call} runs before the SSRF guard"


def test_the_youtube_fetch_does_not_blindly_follow_redirects():
    """Defence in depth: a redirect is a NEW url this node did not choose, which is why every other
    fetcher here hand-rolls its hops."""
    code = _code(SearchService._fetch_youtube_content)
    assert "follow_redirects=False" in code
    assert "is_safe_url(nxt)" in code, "redirect hops are not re-validated"


def test_the_other_fetchers_still_hand_roll_their_hops():
    """The guard that was already right must stay right."""
    src = inspect.getsource(SearchService)
    assert src.count("follow_redirects=False") >= 3
    assert "too many redirects" in src
