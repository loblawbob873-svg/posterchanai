"""Fediverse object normalisers — Pleroma/Mastodon → one flat dict.

Extracted VERBATIM from the old fedi_timeline_service, which has since been deleted; this is the
proven normalisation logic it was built on. Consumers: fedi_nostr_bridge_service,
fedi_nostr_personal_service, fedi_bridge_identity.
"""
import html as _html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>", re.IGNORECASE)
_EMOJI_SHORTCODE_RE = re.compile(r':([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):')


def _strip_html(raw: str) -> str:
    text = _BREAK_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


def _norm_pleroma(s: dict) -> dict:
    acct_obj = s.get("account") or {}
    media = [{"url": m.get("url"), "mime": ""} for m in (s.get("media_attachments") or []) if m.get("url")]
    # `quote` = a quote-post's quoted status; `reblog` (with empty content) = a plain boost.
    quote = None
    sub = s.get("quote") or s.get("reblog")
    if sub:
        sub_acct = sub.get("account") or {}
        quote = {
            "acct": sub_acct.get("acct") or sub_acct.get("username", "?"),
            "display": sub_acct.get("display_name") or sub_acct.get("username", ""),
            "text": _strip_html(sub.get("content", "")),
            "html": sub.get("content"),
            "emojis": _emoji_url_map(sub_acct.get("emojis")),
            "content_emojis": _emoji_url_map(sub.get("emojis")),
        }
    return {
        "id": s.get("id"),
        "uri": s.get("uri") or s.get("url"),   # always present for local statuses
        "author": {
            "acct": acct_obj.get("acct") or acct_obj.get("username", "?"),
            "display": acct_obj.get("display_name") or acct_obj.get("username", ""),
            "avatar_url": acct_obj.get("avatar"),
            "url": acct_obj.get("url"),        # profile page on the author's instance
            "emojis": _emoji_url_map(acct_obj.get("emojis")),
        },
        "text": _strip_html(s.get("content", "")),
        "html": s.get("content"),              # Pleroma content is already HTML
        "media": media,
        "quote": quote,
        "content_emojis": _emoji_url_map(s.get("emojis")),   # custom emoji used in the content
        "url": s.get("url") or s.get("uri"),   # human URL to the post/thread
        "in_reply_to_id": s.get("in_reply_to_id"),  # parent status id (for thread reply chains)
        "replies_count": s.get("replies_count") or 0,
        "created_at": s.get("created_at"),
    }


def _norm(platform: str, raw: dict) -> dict:
    return _norm_pleroma(raw)


def _canonical_uri(platform: str, instance_url: str, post: dict) -> str | None:
    """The cross-instance AP URI used to resolve a post on a member's own instance and to
    dedup federated copies."""
    return post.get("uri") or None


# --- rendering --------------------------------------------------------------

# Mention/profile anchors in post HTML. We strip these to plain text: clients that unfurl a
# fediverse profile link (`/users/x` or `/@x`) render the user's whole profile card + bio below
# every post — unwanted bloat. Removing the <a> (keeping the @name text) leaves the mention
# readable without a previewable link.


def emoji_tags_for(text: str, emap: dict, limit: int = 30) -> list:
    """NIP-30 ['emoji', shortcode, url] tags for every :shortcode: in `text` that has a url in `emap`.
    Deduped + bounded. Shared by the note mirror (kind-1 content) and the puppet profile builder (kind-0
    name/bio) so shortcode matching can't drift between them."""
    if not text or not emap:
        return []
    out, seen = [], set()
    for sc in _EMOJI_SHORTCODE_RE.findall(text):
        if sc in seen:
            continue
        url = emap.get(sc)
        if url:
            out.append(["emoji", sc, url])
            seen.add(sc)
        if len(out) >= limit:
            break
    return out


def _emoji_url_map(raw) -> dict:
    """Normalize a platform emoji field (a list of {shortcode,url}, or a {name: url} dict)
    to {shortcode: url}. Both shapes are accepted — instances differ."""
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if v}
    if isinstance(raw, list):
        out = {}
        for e in raw:
            sc = e.get("shortcode") or e.get("name")
            url = e.get("url") or e.get("static_url")
            if sc and url:
                out[sc] = url
        return out
    return {}
