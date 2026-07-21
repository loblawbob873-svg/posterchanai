"""Fediverse object normalisers — Pleroma/Mastodon and Misskey → one flat dict.

Extracted VERBATIM from fedi_timeline_service so the Nostr bridge doesn't depend on a Matrix module.
Nothing here touches Matrix; it was only ever co-located. Consumers: fedi_nostr_bridge_service,
fedi_nostr_personal_service, fedi_bridge_identity.
"""
import html as _html
import re
from urllib.parse import urlparse

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>", re.IGNORECASE)
_EMOJI_SHORTCODE_RE = re.compile(r':([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):')


def _strip_html(raw: str) -> str:
    text = _BREAK_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


def _norm_misskey(n: dict) -> dict:
    user = n.get("user") or {}
    name = user.get("username", "?")
    host = user.get("host")
    acct = f"{name}@{host}" if host else name
    media = [{"url": f.get("url"), "mime": f.get("type", "")} for f in (n.get("files") or []) if f.get("url")]
    # A renote with text is a quote; a renote without text is a plain boost. Either way capture
    # the quoted/boosted note so it isn't lost when the post is rendered.
    quote = None
    rn = n.get("renote")
    if rn:
        ru = rn.get("user") or {}
        rname = ru.get("username", "?")
        rhost = ru.get("host")
        quote = {
            "acct": f"{rname}@{rhost}" if rhost else rname,
            "display": ru.get("name") or rname,
            "text": rn.get("text") or "",
            "html": None,
            "emojis": _emoji_url_map(ru.get("emojis")),
            "content_emojis": _emoji_url_map(rn.get("emojis")),
        }
    return {
        "id": n.get("id"),
        "uri": n.get("uri") or n.get("url"),   # local notes carry neither; canonicalized later
        "author": {
            "acct": acct,
            "display": user.get("name") or name,
            "avatar_url": user.get("avatarUrl"),
            "url": user.get("url"),            # remote users only; local synthesized later
            "emojis": _emoji_url_map(user.get("emojis")),
        },
        "text": n.get("text") or "",
        "html": None,                          # Misskey text is MFM/plain → render to HTML
        "media": media,
        "quote": quote,
        "content_emojis": _emoji_url_map(n.get("emojis")),   # custom emoji used in the note text
        "url": n.get("url"),                   # human URL to the post (remote notes only)
        "in_reply_to_id": n.get("replyId"),    # parent note id (for proper thread reply chains)
        "replies_count": n.get("repliesCount") or 0,
        "created_at": n.get("createdAt"),
    }


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
    return _norm_misskey(raw) if platform == "misskey" else _norm_pleroma(raw)


def _canonical_uri(platform: str, instance_url: str, post: dict) -> str | None:
    """The cross-instance AP URI used to resolve a post on a member's own instance and to
    dedup federated copies. Misskey local notes have no `uri`, so synthesize the canonical one."""
    if post.get("uri"):
        return post["uri"]
    if platform == "misskey" and post.get("id"):
        return f"{instance_url.rstrip('/')}/notes/{post['id']}"
    return None


# --- rendering --------------------------------------------------------------

# Mention/profile anchors in post HTML. We strip these to plain text: a Matrix client renders a
# URL preview for a fediverse profile link (`/users/x` or `/@x`), which shows the user's whole
# profile card + bio below every post — unwanted bloat. Removing the <a> (keeping the @name text)
# leaves the mention readable without a previewable link.


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
    """Normalize a platform emoji field (Pleroma list of {shortcode,url}; Misskey dict
    {name: url} or list) to {shortcode: url}."""
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
