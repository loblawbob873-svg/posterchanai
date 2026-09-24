"""Fediverse text helpers: HTML flattened to text, and custom emoji (NIP-30 tags) from a platform's
emoji list. Used by the ActivityPub server (inbox/convert) and the puppet identity code.

(The Pleroma status normalisers that lived here went with the Pleroma bridge.)
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
    # https only: these URLs reach every reader's browser as image sources, so a plain-http or
    # LAN address would let a remote server track readers or probe their network.
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if _https(v)}
    if isinstance(raw, list):
        out = {}
        for e in raw:
            if not isinstance(e, dict):
                continue
            sc = e.get("shortcode") or e.get("name")
            url = e.get("url") or e.get("static_url")
            if sc and _https(url):
                out[sc] = url
        return out
    return {}


def _https(url) -> bool:
    return isinstance(url, str) and url.startswith("https://") and len(url) < 2048
