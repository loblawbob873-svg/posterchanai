"""Fediverse text helpers: HTML flattened to text, and custom emoji (NIP-30 tags) from a platform's
emoji list. Used by the ActivityPub server (inbox/convert) and the puppet identity code.

(The Pleroma status normalisers that lived here went with the Pleroma bridge.)
"""
import html as _html
import re
from html.parser import HTMLParser

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>", re.IGNORECASE)
_EMOJI_SHORTCODE_RE = re.compile(r':([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):')


def _strip_html(raw: str) -> str:
    text = _BREAK_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


class _Flattener(HTMLParser):
    """A post's HTML as the text a Nostr client shows. See html_to_text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.lists, self.links = [], [], []

    def _text(self) -> str:
        return "".join(self.out)

    def _newline(self):
        t = self._text()
        if t and not t.endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "br":
            self.out.append("\n")
        elif tag in ("ol", "ul"):
            self._newline()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._newline()
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.out.append(f"{self.lists[-1][1]}. ")
            else:
                self.out.append("• ")
        elif tag == "a":
            self.links.append((str(a.get("href") or ""), " ".join([str(a.get("class") or ""), str(a.get("rel") or "")]),
                               len(self._text())))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"):
            self._newline()
        elif tag in ("ol", "ul"):
            if self.lists:
                self.lists.pop()
            self._newline()
        elif tag == "a" and self.links:
            href, cls, start = self.links.pop()
            t = self._text()
            label = t[start:]
            url = _link_target(href, label, cls)
            if url is not None:
                self.out = [t[:start], url]

    def handle_data(self, data):
        self.out.append(data)


def _link_target(href: str, label: str, cls: str):
    """What replaces a link's visible words, or None to keep them. A mention or hashtag stays its words
    (the mention is linked to a person later, by activitypub/mentions.py). A link whose words ARE its
    address stays the address. Anything else -- "View post", "my blog" -- keeps its words AND gains the
    address, which was being thrown away: blockbot's Top Posts arrived as ten "🔗 View post"s pointing
    nowhere."""
    if re.search(r"\b(mention|hashtag|tag)\b", cls) or not re.match(r"https?://", href, re.I):
        return None
    words = " ".join(label.split())
    bare = lambda u: re.sub(r"^https?://(www\.)?", "", u.strip().rstrip("/"), flags=re.I)
    if not words or bare(words).rstrip("…").rstrip(".") and bare(href).startswith(bare(words).rstrip("…").rstrip(".")):
        return href
    return f"{words} {href}"


def html_to_text(raw: str) -> str:
    """A post body's HTML flattened for Nostr: paragraphs and line breaks kept, ordered lists numbered,
    unordered ones bulleted, and every link's ADDRESS kept (`_strip_html` keeps only its words, which is
    right for a name or a label and wrong for a post). Falls back to `_strip_html` on anything the parser
    cannot take."""
    try:
        f = _Flattener()
        f.feed(raw or "")
        f.close()
        text = re.sub(r"[ \t]+\n", "\n", f._text())
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception:
        return _strip_html(raw)


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
