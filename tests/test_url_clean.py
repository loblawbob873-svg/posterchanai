"""The composer's URL tracking removal, run as the SHIPPED code.

Run: venv-unified/bin/python -m pytest tests/test_url_clean.py

node runs static/js/client/urlclean.js itself, so these assert the real module rather than a Python
re-implementation of my own assumptions about it.

The failure mode this guards against is NOT "a tracker survived" — that is a missing table entry,
visible and harmless. It is the opposite: cleaning something that was load-bearing, which turns a
link the user deliberately pasted into a 404 or a paywall, silently, at post time, on every device
that has the setting on. So the "must NOT change" cases below matter more than the strip cases:

  * nytimes.com `unlocked_article_code` IS the gift link. `smid` next to it is the tracker.
  * a parameter is only a tracker on the hosts that use it as one — `si`, `source`, `ref`, `s`, `t`
    are ordinary content parameters elsewhere, so they are host-scoped and must not leak global.
  * a URL with nothing to strip must come back BYTE-IDENTICAL, never re-serialized: round-tripping
    through URL() alone rewrites escaping (`+` vs `%20`) and lowercases the host, which would show
    up as a diff on every link in the post.
  * a URL in prose ends where the sentence does, but `…/Foo_(bar)` opens its own bracket — a
    blanket trailing-punctuation trim breaks exactly those.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(ROOT, "static", "js", "client", "urlclean.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(js):
    """Evaluate `js` with PCUrlClean loaded as `U`; the script prints JSON, we return it."""
    src = f"const U = require({json.dumps(MODULE)});\n{js}"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def clean(url):
    return _run(f"console.log(JSON.stringify(U.clean({json.dumps(url)})))")


def clean_text(text):
    return _run(f"console.log(JSON.stringify(U.cleanText({json.dumps(text)})))")


# ── parameters that must be stripped ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,want", [
    # the global list + prefix families
    ("https://ex.com/a?utm_source=x&utm_medium=y&id=7", "https://ex.com/a?id=7"),
    ("https://ex.com/a?fbclid=abc", "https://ex.com/a"),
    ("https://ex.com/a?gclid=1&msclkid=2&ttclid=3&keep=4", "https://ex.com/a?keep=4"),
    ("https://ex.com/a?mtm_campaign=x&pk_source=y&z=1", "https://ex.com/a?z=1"),
    # how a link most often arrives here: shared out of an RSS/news item
    ("https://news.site/story?id=9&utm_source=rss1.0mainlinkanon&utm_medium=feed&utm_campaign=rss",
     "https://news.site/story?id=9"),
    # case-insensitive: `UTM_Source` and `WT.mc_id` both occur in the wild
    ("https://ex.com/a?UTM_Source=Newsletter&b=2", "https://ex.com/a?b=2"),
    # the whole query going away takes the `?` with it
    ("https://ex.com/a?utm_source=x", "https://ex.com/a"),
    # host-scoped
    ("https://www.youtube.com/watch?v=abc&si=XYZ&t=42", "https://www.youtube.com/watch?v=abc&t=42"),
    ("https://youtu.be/abc?si=XYZ", "https://youtu.be/abc"),
    ("https://open.spotify.com/track/abc?si=deadbeef", "https://open.spotify.com/track/abc"),
    ("https://x.com/u/status/123?s=20&t=abc", "https://x.com/u/status/123"),
    ("https://old.reddit.com/r/x/comments/1/t/?share_id=9", "https://old.reddit.com/r/x/comments/1/t/"),
    # subdomains inherit the host rule (m./music. are the same site)
    ("https://m.youtube.com/watch?v=abc&si=Q", "https://m.youtube.com/watch?v=abc"),
    # a tracker-only fragment goes; the path rule keeps the rest
    ("https://ex.com/a#Echobox=1699", "https://ex.com/a"),
])
def test_strips(url, want):
    assert clean(url) == want


def test_amazon_path_tracker():
    """Amazon's beacon is a PATH segment, not a query parameter. /dp/<ASIN> always resolves."""
    got = clean("https://www.amazon.com/Some-Title/dp/B08N5WRWNW/ref=sr_1_3?keywords=usb&qid=9&sr=8-3")
    assert got == "https://www.amazon.com/dp/B08N5WRWNW?keywords=usb"


# ── things that must NOT be touched ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    # nothing to strip → byte-identical, not re-serialized
    "https://ex.com/a?id=7",
    "https://ex.com/a",
    "https://EX.com/A?B=1",                                  # host case preserved
    "https://ex.com/search?q=hello+world",                   # `+` not re-encoded to %20
    "https://ex.com/a?x=%7Efoo",                             # %7E not normalised to ~
    "https://en.wikipedia.org/wiki/Foo_(bar)",
    "https://ex.com/a#section-2",                            # a real anchor is not a tracker
    "https://ex.com/a#:~:text=quoted",                       # scroll-to-text is user intent
    # not http(s) — a nostr:, mailto: or relative link is returned untouched
    "nostr:nevent1abc",
    "mailto:someone@ex.com?subject=utm_source",
    # host-scoped names must NOT leak to other hosts
    "https://ex.com/a?si=session123",
    "https://ex.com/a?source=partner",
    "https://ex.com/a?ref=friend",
    "https://ex.com/a?s=query&t=type",
    "https://ex.com/dp/B08N5WRWNW/ref=whatever",             # the Amazon path rule is Amazon-only
])
def test_leaves_alone(url):
    assert clean(url) == url


def test_gift_link_survives_its_tracker():
    """The one that must never regress: strip the tracker, keep the thing that unlocks the article."""
    got = clean("https://www.nytimes.com/2024/01/01/x.html?unlocked_article_code=ABC123&smid=tw-share")
    assert got == "https://www.nytimes.com/2024/01/01/x.html?unlocked_article_code=ABC123"


def test_garbage_is_returned_unchanged():
    for bad in ["http://", "https://[", "https://ex.com:99999/a", ""]:
        assert clean(bad) == bad


# ── click wrappers ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,want", [
    ("https://www.google.com/url?q=https%3A%2F%2Fnews.site%2Fp%3Futm_source%3Dfb&sa=U&ved=2ah",
     "https://news.site/p"),
    ("https://l.facebook.com/l.php?u=https%3A%2F%2Fex.org%2Fx%3Ffbclid%3D123&h=AT1",
     "https://ex.org/x"),
    ("https://out.reddit.com/?url=https%3A%2F%2Fex.org%2Fy",
     "https://ex.org/y"),
    ("https://t.umblr.com/redirect?z=https%3A%2F%2Fex.org%2Fz&t=abc",
     "https://ex.org/z"),
    ("https://vk.com/away.php?to=https%3A%2F%2Fex.org%2Fv",
     "https://ex.org/v"),
    ("https://href.li/?https://ex.org/a?fbclid=1",
     "https://ex.org/a"),
    # Outlook safelinks — very common in anything pasted out of a work inbox
    ("https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Freal.example%2Fp%3Fgclid%3D9&data=05",
     "https://real.example/p"),
    # Proofpoint puts the destination between __ markers, not in a parameter
    ("https://urldefense.com/v3/__https://real.example/p?utm_source=mail__;!!abc$",
     "https://real.example/p"),
    # AMP prefixes: the `s` is the destination's scheme, not part of the host
    ("https://www.google.com/amp/s/www.cnn.com/2024/01/01/x/index.html",
     "https://www.cnn.com/2024/01/01/x/index.html"),
    ("https://pub-cdn.cdn.ampproject.org/v/s/ex.org/story?utm_source=amp",
     "https://ex.org/story"),
])
def test_unwraps(url, want):
    assert clean(url) == want


def test_nested_wrappers_unwrap_all_the_way():
    """Wrappers nest — a safelink around a google /url around the article."""
    inner = "https%3A%2F%2Fnews.site%2Fp%3Futm_source%3Dx"
    mid = f"https%3A%2F%2Fwww.google.com%2Furl%3Fq%3D{inner.replace('%', '%25')}"
    assert clean(f"https://nam01.safelinks.protection.outlook.com/?url={mid}") == "https://news.site/p"


def test_wrapper_that_points_at_itself_terminates():
    """A malformed ?u=<same url> must hit the depth cap, not spin — this runs on every keystroke's
    worth of draft at post time."""
    same = "https://l.facebook.com/l.php?u=https%3A%2F%2Fl.facebook.com%2Fl.php%3Fu%3Dhttps%253A%252F%252Fex.org%252Fa"
    assert clean(same) == "https://ex.org/a"


@pytest.mark.parametrize("url", [
    "https://l.facebook.com/l.php?u=not-a-url",
    "https://l.facebook.com/l.php?u=javascript%3Aalert(1)",
    "https://nam01.safelinks.protection.outlook.com/?url=/relative/path&data=05",
])
def test_wrapper_holding_a_non_url_is_left_alone(url):
    """A wrapper parameter carrying a relative path, a `javascript:` payload or plain garbage must
    leave the link ALONE — never substitute that string for the URL the user pasted."""
    assert clean(url) == url


@pytest.mark.parametrize("url", [
    # a host whose FIRST LABEL is the brand is not the brand: /^amazon\./ and /google\./ patterns
    # both fire on these, which is why the host tables are enumerated instead.
    "https://amazon.example.com/dp/B012345678/ref=keepme",
    "https://google.com.evil.com/amp/s/phish.example/x",
])
def test_brand_lookalike_hosts_do_not_get_brand_rules(url):
    assert clean(url) == url


def test_a_stray_percent_does_not_throw():
    """decodeURIComponent('100%') throws. This runs on the post path with auto-clean on, so a throw
    here doesn't mangle a link — it stops the post being published at all."""
    assert clean("https://ex.com/a?x=100%&utm_source=q") == "https://ex.com/a?x=100%"


# ── whole-post cleaning ───────────────────────────────────────────────────────────────────────────

def test_clean_text_reports_what_changed():
    r = clean_text("read this https://ex.com/a?utm_source=t. and (https://ex.com/b?fbclid=9) ok")
    assert r["text"] == "read this https://ex.com/a. and (https://ex.com/b) ok"
    assert r["count"] == 2
    assert r["changes"][0] == ["https://ex.com/a?utm_source=t", "https://ex.com/a"]


def test_clean_text_keeps_sentence_punctuation_and_real_brackets():
    """The trailing `.` belongs to the sentence; the `)` in the Wikipedia title belongs to the URL."""
    r = clean_text("see https://en.wikipedia.org/wiki/Foo_(bar)?utm_source=x, and that's it.")
    assert r["text"] == "see https://en.wikipedia.org/wiki/Foo_(bar), and that's it."
    assert r["count"] == 1


def test_clean_text_is_a_noop_with_nothing_to_do():
    for text in ["", "no links here at all", "https://ex.com/a?id=1 is fine", "nostr:nevent1abc"]:
        r = clean_text(text)
        assert r["text"] == text and r["count"] == 0


def test_clean_text_leaves_markdown_link_text_alone():
    r = clean_text("[the article](https://ex.com/a?utm_source=x)")
    assert r["text"] == "[the article](https://ex.com/a)"


def test_clean_text_handles_many_links_and_multiline():
    r = clean_text("https://a.com/1?utm_source=x\nhttps://b.com/2?fbclid=y\nhttps://c.com/3?id=z")
    assert r["count"] == 2
    assert r["text"] == "https://a.com/1\nhttps://b.com/2\nhttps://c.com/3?id=z"
