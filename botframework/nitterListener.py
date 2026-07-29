"""
Nitter RSS → fediverse poster.

Polls a list of Nitter RSS feeds and posts new items to the configured
room. Feeds are defined in bots_config.py as the `nitter_feeds` array on the
bot, each entry being {"room": "!id:server", "rss": "https://nitter.../rss"},
passed to this process as the NITTER_FEEDS env var (JSON).

On the first time a feed is seen, its current items are recorded as "seen"
WITHOUT posting, so the bot doesn't dump the whole backlog on
startup — only genuinely new posts after that are sent.
"""
import os
import sys
import json
import time

import requests
from lxml import etree

# Ensure the script directory is in the Python path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from config import PLEROMA_ENDPOINT, NOSTR_NSEC

# Per-bot feeds, set by botctl/the installer from the bot's `nitter_feeds`.
NITTER_FEEDS = json.loads(os.getenv("NITTER_FEEDS", "[]"))
NITTER_POLL_SECONDS = int(os.getenv("NITTER_POLL_SECONDS", "300"))
# Fire-rate throttle: post at most N items per feed per poll (the rest stay unseen and
# drain on later cycles — nothing is dropped, just spread out), with a pause between
# posts so a backlogged feed doesn't dump a wall of notes / trip relay rate limits.
NITTER_MAX_POSTS_PER_CYCLE = int(os.getenv("NITTER_MAX_POSTS_PER_CYCLE", "3"))
NITTER_POST_DELAY = int(os.getenv("NITTER_POST_DELAY", "5"))

# Public Nitter instances are flaky: they rate-limit, time out, 403, or serve a Cloudflare
# challenge page instead of RSS, and the working set rotates constantly. So a feed's pinned
# host is just a starting point — we fail over across this list until one returns a REAL feed,
# and remember the last working instance so the next poll tries it first. Override/extend with
# the NITTER_INSTANCES env (comma/space separated hostnames), highest-preference first.
_DEFAULT_INSTANCES = ["nitter.net", "nitter.privacyredirect.com", "xcancel.com",
                      "nitter.poast.org", "lightbrd.com", "nitter.space"]
NITTER_INSTANCES = [h.strip().rstrip("/") for h in
                    os.getenv("NITTER_INSTANCES", ",".join(_DEFAULT_INSTANCES))
                    .replace(",", " ").split() if h.strip()]
NITTER_FETCH_TIMEOUT = int(os.getenv("NITTER_FETCH_TIMEOUT", "12"))  # per-instance, so failover is quick
_last_good_instance = None  # remembered across polls; tried first next time

# The bot inherits HTTP(S)_PROXY=Tor from its env (needed for fediverse federation), and
# `requests` honours those by default — which routed nitter through Tor. nitter/Cloudflare
# throttles & challenges Tor exits, causing the intermittent 30s read-timeouts and 403s. nitter
# is just public RSS, so fetch it DIRECT: a session with trust_env=False ignores the proxy env.
# Set NITTER_USE_PROXY=1 to route via the proxy again (e.g. if the host IP ever gets blocked).
_session = requests.Session()
_session.trust_env = os.getenv("NITTER_USE_PROXY", "").lower() in ("1", "true", "yes")

_fedi_post = None
_fedi_post_image = None
_is_nostr = False
if PLEROMA_ENDPOINT:
    from pleroma import post_to_fediverse as _fedi_post, post_image_to_fediverse as _fedi_post_image
elif NOSTR_NSEC:
    # Nostr has one post primitive: post_image_to_fediverse(text, image_bytes=None) handles
    # both the text-only and text+image cases, so both hooks point at it.
    from nostr import post_image_to_fediverse as _fedi_post_image
    _fedi_post = _fedi_post_image
    _is_nostr = True

# Nitter→Nostr posts are tagged with a hashtag (default #news) so they collect in that
# hashtag feed. Overridable per-bot via NITTER_NOSTR_HASHTAG; Pleroma is
# unaffected. The literal #tag is kept in the body (readable / client-linkified) AND a real
# NIP-12 `t` tag is added by the Nostr poster so it also lands in indexed #hashtag feeds.
NITTER_NOSTR_HASHTAG = (os.getenv("NITTER_NOSTR_HASHTAG", "news") or "news").lstrip("#").strip() or "news"


def _news_text(text):
    """Append the Nostr hashtag to a post's body (no-op off the Nostr path / if already present)."""
    if not _is_nostr:
        return text
    tag = "#" + NITTER_NOSTR_HASHTAG
    return text if tag.lower() in (text or "").lower() else ((text or "") + "\n\n" + tag)


def _news_kwargs():
    """Extra kwargs for the Nostr poster so the post carries a real NIP-12 `t` hashtag tag."""
    return {"hashtags": [NITTER_NOSTR_HASHTAG]} if _is_nostr else {}

_STATE_FILE = os.path.join(script_dir, ".nitter_seen.json")
_MAX_SEEN_PER_FEED = 300  # cap stored GUIDs per feed so the state file stays small


def _load_state():
    """Return {feed_url: [seen_guid, ...]} from disk, or {} on first run/error."""
    try:
        with open(_STATE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        print(f"[nitter] Could not read state file: {e}", flush=True)
        return {}


def _save_state(state):
    """Persist seen-GUID state atomically."""
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        print(f"[nitter] Could not write state file: {e}", flush=True)


def _handle_from_rss(rss_url):
    """Best-effort @handle from a Nitter RSS URL (https://nitter.net/NAME/rss)."""
    try:
        parts = [p for p in rss_url.split("/") if p]
        # .../NAME/rss  -> NAME is the segment before "rss"
        if parts and parts[-1].lower() == "rss" and len(parts) >= 2:
            return parts[-2]
        return parts[-1] if parts else "feed"
    except Exception:
        return "feed"


# Instance gate/whitelist sentinels that arrive as a 200 RSS body but contain no real
# tweets — must never be parsed or posted. Matched case-insensitively against the raw body
# (fetch level) and individual item titles (defensive, in case a feed mixes them in).
# Kept to phrases distinctive to the gate page ("RSS reader not yet whitelisted! Plain
# request with just ID will be ignored!") so a normal tweet can't false-positive the feed.
_GATE_SENTINELS = ("not yet whitelist", "plain request with just id")


def _is_gate_page(content) -> bool:
    try:
        text = (content if isinstance(content, str) else content.decode("utf-8", "ignore")).lower()
    except Exception:
        return False
    return any(s in text for s in _GATE_SENTINELS)


def _should_skip(item):
    """True if a Nitter RSS item should not be posted.

    We keep only an account's own original, text-bearing tweets and drop:
      - retweets  — Nitter prefixes the title either with "RT by @user:" (a
        native retweet) or "RT @handle:" (a classic/manual retweet); both are
        someone else's content and must be dropped.
      - replies   — Nitter prefixes the title with "R to @user:"
      - image/media-only tweets — Nitter builds the title from the tweet text,
        so a tweet with no text of its own has an empty title.
      - GIF-only tweets — a text-less animated-GIF tweet gets the literal
        placeholder title "Gif" (Nitter's media label), which slips past the
        empty-title check; these are low-value/spammy reaction posts.

    Filtering on the title is the reliable signal across Nitter instances.
    """
    title = (item.findtext("title") or "").strip()
    if not title or title.lower() == "gif":
        return True
    if _is_gate_page(title):  # gate message that leaked in as an item
        return True
    return (title.startswith("RT by ") or title.startswith("RT @")
            or title.startswith("R to "))


_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


def _parse_description(desc):
    """Extract (text, media_url) from a Nitter item's HTML <description>.

    The description is CDATA HTML: the tweet text in <p>; media as an <img src> (photos)
    or a <video poster=...> thumbnail (videos/GIFs); and, for quote-tweets, a <blockquote>
    holding the quoted post. The TEXT is always this tweet's only (the blockquote is
    dropped). For the MEDIA we prefer this tweet's own image, then its own video
    thumbnail, and finally fall back to the quoted tweet's media — otherwise video tweets
    and quote-tweets (whose only picture lives in the quoted post) render as a bare
    text card with no embed. Returns ("", "") on any parse failure.
    """
    if not desc or not desc.strip():
        return "", ""
    try:
        from lxml import html as _lxml_html
        frag = _lxml_html.fromstring(desc)
        # Capture the quoted tweet's media BEFORE dropping the blockquote — used only as
        # a last resort when this tweet carries no media of its own.
        quoted = frag.xpath("//blockquote//img/@src") + frag.xpath("//blockquote//video/@poster")
        for bq in frag.xpath("//blockquote"):
            bq.getparent().remove(bq)
        text = frag.text_content().strip()
        # This tweet's own media: a real image first, else a video's poster thumbnail.
        own = frag.xpath("//img/@src") + frag.xpath("//video/@poster")
        media = own or quoted
        media_url = media[0].strip() if media else ""
        return text, media_url
    except Exception:
        return "", ""


def _fmt_pubdate(pubdate):
    """RFC-822 pubDate → 'May 31, 2026' (best-effort; '' on failure)."""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pubdate).strftime("%b %d, %Y")
    except Exception:
        return ""


def _resolve_nitter_pic(url):
    """Resolve a Nitter /pic/ proxy URL to the underlying Twitter CDN URL, which is
    far more reliable than the (often dead) Nitter media proxy.

    Nitter encodes the target after /pic/ in one of three shapes, all of which occur
    in the wild (sometimes within the same instance):
      - a full URL:        /pic/https%3A%2F%2Fpbs.twimg.com%2F...   -> use as-is
      - a host+path:       /pic/pbs.twimg.com%2Fprofile_images%2F.. -> add scheme only
      - a bare CDN path:   /pic/media%2F...                         -> add pbs.twimg.com
    The host+path form is what profile pictures usually use; blindly prefixing
    pbs.twimg.com there produced a doubled host (pbs.twimg.com/pbs.twimg.com/...) that
    404'd, which is why avatars went missing while tweet media (bare path) worked.
    Returns the input unchanged if it isn't a /pic/ URL."""
    if not url or "/pic/" not in url:
        return url or ""
    from urllib.parse import unquote
    tail = unquote(url.split("/pic/", 1)[1]).lstrip("/")
    if tail.startswith(("http://", "https://")):
        return tail
    # A leading "host.tld/..." segment (contains a dot) is already host-qualified, so
    # just prepend the scheme; otherwise it's a bare CDN path under pbs.twimg.com.
    first = tail.split("/", 1)[0]
    return ("https://" + tail) if "." in first else ("https://pbs.twimg.com/" + tail)


def _download(url, max_bytes=20_000_000):
    """Fetch an image URL → (bytes, content_type) or (None, None) on any failure.

    Streams with a size cap so a malicious/huge response can't exhaust memory in the
    long-running poller (images are well under the cap)."""
    if not url:
        return None, None
    try:
        with _session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, stream=True) as r:
            if not r.ok:
                return None, None
            ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            data = bytearray()
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > max_bytes:
                    print(f"[nitter] media exceeds {max_bytes}B, skipping {url[:60]}", flush=True)
                    return None, None
            return (bytes(data), ct) if data else (None, None)
    except Exception as e:
        print(f"[nitter] download failed for {url[:60]} ({e})", flush=True)
    return None, None


def _instance_order():
    """Hostnames to try, last-known-good first."""
    order = [h for h in NITTER_INSTANCES if h != _last_good_instance]
    return ([_last_good_instance] if _last_good_instance else []) + order


def _fetch_items(rss_url):
    """Fetch a Nitter RSS feed, failing over across instances until one returns a real feed.

    Public instances rotate/rate-limit/serve challenge pages, so we try the configured host
    plus the fallback list (last-good first), validating that each response is an actual RSS
    feed (`<channel>` present) — a 200 OK can still be a Cloudflare challenge page. Returns a
    list of item dicts (newest first); empty if every instance failed.
    """
    global _last_good_instance
    handle = _handle_from_rss(rss_url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; posterchan-nitter/1.0)"}
    last_err = "no instances configured"
    for host in _instance_order():
        url = f"https://{host}/{handle}/rss"
        try:
            resp = _session.get(url, headers=headers, timeout=NITTER_FETCH_TIMEOUT)
            resp.raise_for_status()
            # Some instances (notably nitter.net) return a 200 *valid-RSS* body whose only
            # item is a gate message ("RSS reader not yet whitelisted! Plain request with
            # just ID will be ignored!"). It has a <channel>, so it slips past the check below
            # and would get POSTED verbatim. Treat the gate sentinel as a failure → fail over.
            if _is_gate_page(resp.content):
                last_err = f"{host}: RSS reader not whitelisted (gate page)"
                continue
            root = etree.fromstring(resp.content, parser=etree.XMLParser(recover=True))
            if root is None or root.find(".//channel") is None:
                last_err = f"{host}: not a valid RSS feed (blocked/challenge page)"
                continue
        except Exception as e:
            last_err = f"{host}: {e}"
            continue
        if host != _last_good_instance:
            print(f"[nitter] @{handle}: using instance {host}", flush=True)
            _last_good_instance = host
        return _parse_feed(root)
    print(f"[nitter] @{handle}: all {len(NITTER_INSTANCES)} instance(s) failed "
          f"(last error — {last_err})", flush=True)
    return []


def _parse_feed(root):
    """Parse a validated Nitter RSS `root` into item dicts (newest first).

    Retweets, replies, and image-only (text-less) posts are skipped — only the
    account's own original tweets that carry text are kept. Each item also carries
    the tweet text, first media URL, author handle and date used to render the
    post-card image.
    """
    # Channel-level <image> carries the account's profile picture and display name
    # ("Display Name / @handle"), reused for every item's card.
    avatar_url, channel_name = "", ""
    chan = root.find(".//channel")
    img = chan.find("image") if chan is not None else None
    if img is not None:
        avatar_url = (img.findtext("url") or "").strip()
        ctitle = (img.findtext("title") or "").strip()
        channel_name = ctitle.split(" / ", 1)[0].strip() if " / " in ctitle else ctitle

    items = []
    for item in root.findall(".//item"):
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        if not guid:
            continue
        if _should_skip(item):
            continue
        title = (item.findtext("title") or "").strip()
        text, media_url = _parse_description(item.findtext("description") or "")
        author = (item.findtext(_DC_CREATOR) or "").strip().lstrip("@")
        items.append({
            "guid": guid,
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            # Prefer the full description text; fall back to the (often truncated) title.
            "text": text or title,
            "media_url": media_url,
            "author": author,
            "display_name": channel_name or author,
            "avatar_url": avatar_url,
            "timestamp": _fmt_pubdate(item.findtext("pubDate") or ""),
        })
    return items


def _format_post(handle, item):
    """Build the message text for a single feed item.

    The link is left bare (not wrapped in backticks) so it renders as a
    clickable link on the
    fediverse.
    link into a non-clickable <code> span, so it is not used here.
    """
    title = item["title"]
    link = item["link"]
    text = f"🐦 **@{handle}**\n\n{title}"
    if link:
        text += f"\n\n{link}"
    return text


def _format_caption(handle, item):
    """Short caption posted beneath the card image: the handle and a clickable
    link to the source post (left bare so it stays clickable everywhere)."""
    link = item.get("link", "")
    cap = f"🐦 @{handle}"
    if link:
        cap += f"\n\n{link}"
    return cap


def _render_card(handle, item):
    """Render the tweet as a post-card PNG via the posterchanai backend (which
    screenshots HTML we build). Downloads any media here so the server does no
    outbound fetch. Returns PNG bytes, or None to signal a text+link fallback.
    """
    try:
        from posterchanai_api import render_post_card
    except Exception as e:
        print(f"[nitter] post-card unavailable ({e}); using text+link", flush=True)
        return None

    # Tweet media and the author's profile picture (resolved to the Twitter CDN, which
    # outlives the Nitter proxy). Either may fail; the card renders without them.
    media_bytes, media_ct = _download(item.get("media_url"))
    avatar_bytes, avatar_ct = _download(_resolve_nitter_pic(item.get("avatar_url")))

    png, err = render_post_card(
        handle, item.get("text") or item.get("title") or "",
        display_name=item.get("display_name") or item.get("author") or handle,
        timestamp=item.get("timestamp") or "",
        media_bytes=media_bytes, media_ct=media_ct,
        avatar_bytes=avatar_bytes, avatar_ct=avatar_ct,
    )
    if err:
        print(f"[nitter] card render failed ({err}); falling back to text+link", flush=True)
        return None
    return png


def _post_item(feed, handle, item):
    """Post one item to the fediverse.

    Posts the tweet rendered as an image (a "screenshot" of a card we build from the
    RSS data) with the source link beneath it; Nitter's own status pages are empty so
    link previews never render. Falls back to the original text+link post if the card
    can't be produced (e.g. no browser on the backend).
    """
    card = _render_card(handle, item)
    caption = _format_caption(handle, item)

    # Post to the fediverse (Pleroma).
    if _fedi_post is None:
        print("[nitter] Feed has no 'room' and no fediverse is configured; skipping post", flush=True)
        return False
    # Fire-and-forget (no return value); treat as sent.
    if card and _fedi_post_image is not None:
        _fedi_post_image(_news_text(caption), image_bytes=card, **_news_kwargs())
    else:
        _fedi_post(_news_text(_format_post(handle, item)), **_news_kwargs())
    return True


def _process_feed(feed, state):
    """Check one feed config dict and post any new items. Mutates `state`."""
    rss_url = (feed.get("rss") or "").strip()
    if not rss_url:
        print(f"[nitter] Skipping malformed feed entry: {feed}", flush=True)
        return

    try:
        items = _fetch_items(rss_url)
    except Exception as e:
        print(f"[nitter] Failed to fetch {rss_url}: {e}", flush=True)
        return

    if not items:
        return

    seen = state.get(rss_url)
    handle = _handle_from_rss(rss_url)

    # First time we've seen this feed: seed silently, don't post the backlog.
    if seen is None:
        state[rss_url] = [it["guid"] for it in items][:_MAX_SEEN_PER_FEED]
        print(f"[nitter] Seeded @{handle} ({len(items)} existing items, none posted)", flush=True)
        return

    seen_set = set(seen)
    # Post oldest-first so the timeline reads chronologically.
    new_items = [it for it in reversed(items) if it["guid"] not in seen_set]
    if not new_items:
        return

    dest = feed.get("room") or "fediverse"
    # Throttle: post only the oldest NITTER_MAX_POSTS_PER_CYCLE this cycle; the rest stay
    # unseen and drain (chronologically) on subsequent polls.
    batch = new_items[:NITTER_MAX_POSTS_PER_CYCLE] if NITTER_MAX_POSTS_PER_CYCLE > 0 else new_items
    if len(batch) < len(new_items):
        print(f"[nitter] @{handle}: {len(new_items)} new item(s) → {dest} "
              f"(posting {len(batch)} this cycle, {len(new_items) - len(batch)} next)", flush=True)
    else:
        print(f"[nitter] @{handle}: {len(new_items)} new item(s) → {dest}", flush=True)
    for idx, it in enumerate(batch):
        if idx > 0 and NITTER_POST_DELAY > 0:
            time.sleep(NITTER_POST_DELAY)  # pace posts so a burst doesn't flood
        try:
            ok = _post_item(feed, handle, it)
        except Exception as e:
            # A render/format error is specific to THIS item — mark it seen so a
            # single malformed item doesn't wedge the feed forever.
            print(f"[nitter] Error posting {it['guid']}: {e}", flush=True)
            seen.append(it["guid"])
            continue
        if ok:
            seen.append(it["guid"])
        else:
            # Destination temporarily unavailable (e.g. a room send 403 during a
            # membership/federation blip). Leave the item UNSEEN so it retries on
            # the next poll instead of being silently dropped — this was why tweets
            # went to Telegram but never reached the fediverse.
            print(f"[nitter] @{handle}: post not confirmed, will retry next poll", flush=True)

    # Trim and persist
    state[rss_url] = seen[-_MAX_SEEN_PER_FEED:]


def nitter_poster():
    """Main loop: poll all configured Nitter feeds forever."""
    if not NITTER_FEEDS:
        print("[nitter] No feeds configured (NITTER_FEEDS empty); idling.", flush=True)
    else:
        print(f"[nitter] Starting with {len(NITTER_FEEDS)} feed(s), "
              f"poll every {NITTER_POLL_SECONDS}s", flush=True)

    while True:
        if NITTER_FEEDS:
            state = _load_state()
            for feed in NITTER_FEEDS:
                _process_feed(feed, state)
            _save_state(state)
        time.sleep(max(60, NITTER_POLL_SECONDS))


if __name__ == "__main__":
    nitter_poster()
