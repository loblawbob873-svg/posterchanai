"""Nostr ⇄ ActivityPub translation. PURE: no network, no database, no settings.

Everything a translation needs to know about the outside world (who a mentioned pubkey is on the
fediverse, which ActivityPub object a Nostr reply points at) is resolved by the caller and passed
in. That is what lets tests/test_activitypub.py run the real translation over real events.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from app.services.activitypub.config import PUBLIC

AS_CONTEXT = ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1",
              {"Hashtag": "as:Hashtag", "sensitive": "as:sensitive",
               "manuallyApprovesFollowers": "as:manuallyApprovesFollowers", "quoteUrl": "as:quoteUrl",
               "toot": "http://joinmastodon.org/ns#", "Emoji": "toot:Emoji", "blurhash": "toot:blurhash",
               "discoverable": "toot:discoverable", "indexable": "toot:indexable",
               "misskey": "https://misskey-hub.net/ns#", "_misskey_quote": "misskey:_misskey_quote",
               "_misskey_reaction": "misskey:_misskey_reaction",
               "schema": "http://schema.org#", "PropertyValue": "schema:PropertyValue", "value": "schema:value"}]

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# A reference to a person or a post: `nostr:npub1…` (NIP-21) -- or the BARE bech32, which is how most
# clients (and a paste) put one in the text. Only the `nostr:` form was read, so "I tagged an account but
# it showed as an npub on the fediverse": the bare npub went out as 63 characters of text. A bare one
# must stand on its own -- never inside a URL, a path or a word -- and be long enough to be real.
_NOSTR_REF_RE = re.compile(r"(?:nostr:|(?<![\w/:@#.=?&%-]))((?:npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]{50,})", re.I)
_HASHTAG_RE = re.compile(r"(?<![\w/#&])#([A-Za-z0-9_]{1,64})\b")
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|avif)(?:[?#]|$)", re.I)
_VIDEO_EXT = re.compile(r"\.(?:mp4|webm|mov|m4v)(?:[?#]|$)", re.I)
_AUDIO_EXT = re.compile(r"\.(?:mp3|ogg|oga|opus|m4a|wav|flac)(?:[?#]|$)", re.I)


# ------------------------------------------------------------------------------------ addresses

def actor_url(base: str, name: str) -> str:
    return f"{base}/ap/users/{name}"


def object_url(base: str, event_id: str) -> str:
    return f"{base}/ap/objects/{event_id}"


def activity_url(base: str, event_id: str, verb: str = "") -> str:
    return f"{base}/ap/activities/{event_id}" + (f"/{verb.lower()}" if verb else "")


def event_id_from_object_url(base: str, url: str) -> str:
    """The Nostr event id inside one of OUR object URLs, or "" for anything else."""
    prefix = f"{base}/ap/objects/"
    if base and isinstance(url, str) and url.startswith(prefix):
        eid = url[len(prefix):].split("/")[0].split("?")[0].split("#")[0]
        if re.fullmatch(r"[0-9a-f]{64}", eid):
            return eid
    return ""


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts or 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> int:
    try:
        s = str(value or "").strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp()) if s else 0
    except (ValueError, TypeError):
        return 0


# ------------------------------------------------------------------------------------ outbound

def media_type_of(url: str) -> str:
    if _IMAGE_EXT.search(url):
        ext = _IMAGE_EXT.search(url).group(0).lower().lstrip(".").split("?")[0].split("#")[0]
        return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    if _VIDEO_EXT.search(url):
        ext = _VIDEO_EXT.search(url).group(0).lower().lstrip(".").split("?")[0].split("#")[0]
        return "video/quicktime" if ext == "mov" else ("video/mp4" if ext == "m4v" else f"video/{ext}")
    if _AUDIO_EXT.search(url):
        ext = _AUDIO_EXT.search(url).group(0).lower().lstrip(".").split("?")[0].split("#")[0]
        return {"mp3": "audio/mpeg", "m4a": "audio/mp4", "oga": "audio/ogg"}.get(ext, f"audio/{ext}")
    return ""


def attachments(ev: dict) -> list:
    """ActivityPub attachments for the event's media, NIP-92 `imeta` first: the type from its `m`
    (else the file extension), plus `alt` → name, `dim` → width/height and `blurhash`. Only media
    whose type is KNOWN are attachments -- Mastodon drops an attachment it cannot type, and the link
    has already been taken out of the text, so an untyped one simply vanished. Those stay as links."""
    meta = {}
    for t in ev.get("tags") or []:
        if not t or t[0] != "imeta":
            continue
        fields = {}
        for part in t[1:]:
            if isinstance(part, str) and " " in part:
                k, v = part.split(" ", 1)
                fields.setdefault(k, v.strip())
        if fields.get("url"):
            meta[fields["url"]] = fields
    out = []
    for u in media_urls(ev):
        f = meta.get(u, {})
        mt = f.get("m") if re.fullmatch(r"(image|video|audio)/[\w.+-]{1,40}", f.get("m") or "") else ""
        mt = mt or media_type_of(u)
        if not mt:
            continue
        a = {"type": {"image": "Image", "video": "Video", "audio": "Audio"}[mt.split("/")[0]],
             "mediaType": mt, "url": u, "name": (f.get("alt") or "")[:1500] or None}
        dim = re.fullmatch(r"(\d{1,5})x(\d{1,5})", f.get("dim") or "")
        if dim:
            a["width"], a["height"] = int(dim.group(1)), int(dim.group(2))
        if re.fullmatch(r"[0-9A-Za-z#$%*+,\-.:;=?@\[\]^_{|}~]{6,100}", f.get("blurhash") or ""):
            a["blurhash"] = f["blurhash"]
        out.append(a)
    return out


def media_urls(ev: dict) -> list:
    """Attachments: NIP-92 `imeta` urls first, then bare media links in the text, in order, unique."""
    out = []
    for t in ev.get("tags") or []:
        if t and t[0] == "imeta":
            for part in t[1:]:
                if isinstance(part, str) and part.startswith("url "):
                    out.append(part[4:].strip())
    for u in _URL_RE.findall(ev.get("content") or ""):
        u = u.rstrip(").,!?]")
        if media_type_of(u):
            out.append(u)
    seen, uniq = set(), []
    for u in out:
        if u and u not in seen and u.startswith("https://"):
            seen.add(u)
            uniq.append(u)
    return uniq[:8]


def text_to_html(text: str, *, base: str, mentions: dict, drop_urls=(), links: dict = None) -> str:
    """Nostr plain text → the small HTML subset Mastodon renders (p, br, a, span.h-card).

    `mentions` maps a hex pubkey to {"href", "name"} for everyone who has a fediverse address;
    anyone else keeps a link to their profile on this node. `drop_urls` are media links that ride
    as attachments instead, so they are not also printed as bare links."""
    from app.services.nostr import nostr_service   # decode only; no I/O
    drop = set(drop_urls or ())
    last = 0
    tokens = []
    for m in _URL_RE.finditer(text or ""):
        tokens.append((m.start(), m.end(), "url", m.group(0)))
    for m in _NOSTR_REF_RE.finditer(text or ""):
        tokens.append((m.start(), m.end(), "nostr", m.group(1)))
    tokens.sort()
    out = []
    for start, end, kind, value in tokens:
        if start < last:
            continue
        out.append(_escape_with_tags(text[last:start], base))
        if kind == "url":
            url = value.rstrip(").,!?]")
            tail = value[len(url):]
            if url not in drop:
                safe = html.escape(url, quote=True)
                label = html.escape(url.split("://", 1)[1] if "://" in url else url)
                out.append(f'<a href="{safe}" rel="nofollow noopener noreferrer" target="_blank">{label}</a>')
            out.append(html.escape(tail))
        elif not value.lower().startswith(("npub1", "nprofile1")):
            # A reference to a POST (note1/nevent1/naddr1): a link to it -- its fediverse address
            # when it has one (`links`), else this node's page for it -- never 60 characters of
            # bech32 printed as text.
            href = (links or {}).get(value) or f"{base}/{value}"
            label = html.escape(href.split("://", 1)[-1][:60])
            out.append(f'<a href="{html.escape(href, quote=True)}" rel="nofollow noopener noreferrer" '
                       f'target="_blank">{label}</a>')
        else:
            pk = ""
            try:
                pk = nostr_service.to_pubkey_hex(value) or ""
            except Exception:
                pk = ""
            who = mentions.get(pk) if pk else None
            if who:
                name = who.get("name") or ""
                short = name.lstrip("@").split("@")[0]
                # The PROFILE url, not the actor id: a front end knows a mention by the account's `url`
                # (the Mention tag below the text still carries the id, which is what servers match on).
                link = who.get("url") or who["href"]
                out.append(f'<span class="h-card"><a href="{html.escape(link, quote=True)}" '
                           f'class="u-url mention">@<span>{html.escape(short)}</span></a></span>')
            else:
                label = html.escape(value[:14] + "…")
                out.append(f'<a href="{html.escape(base)}/{html.escape(value)}" rel="nofollow noopener">@{label}</a>')
        last = end
    out.append(_escape_with_tags(text[last:], base))
    body = "".join(out).strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)


def _escape_with_tags(chunk: str, base: str) -> str:
    esc = html.escape(chunk)
    return _HASHTAG_RE.sub(
        lambda m: f'<a href="{html.escape(base)}/tags/{m.group(1).lower()}" class="mention hashtag" '
                  f'rel="tag">#<span>{m.group(1)}</span></a>', esc)


def hashtags(ev: dict) -> list:
    tags = {t[1].lower() for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "t" and t[1]}
    tags |= {m.lower() for m in _HASHTAG_RE.findall(ev.get("content") or "")}
    return sorted(tags)[:20]


def emoji_tags(ev: dict, text: str = None) -> list:
    """ActivityPub `Emoji` tags for the custom emoji a Nostr event uses (NIP-30 ['emoji', shortcode,
    url]). The `:shortcode:` stays in the text; Mastodon, Pleroma and Misskey draw the image from the
    matching tag -- without it they print the shortcode ("custom emojis are not displaying"). Only
    shortcodes the text actually uses, and only https images."""
    text = ev.get("content") or "" if text is None else text
    out, seen = [], set()
    for t in ev.get("tags") or []:
        if len(t) < 3 or t[0] != "emoji":
            continue
        sc, url = str(t[1] or "").strip(":"), str(t[2] or "").strip()
        if not sc or sc in seen or not url.startswith("https://") or f":{sc}:" not in text:
            continue
        seen.add(sc)
        out.append({"id": url, "type": "Emoji", "name": f":{sc}:",
                    "icon": {"type": "Image", "mediaType": media_type_of(url) or "image/png", "url": url}})
        if len(out) >= 30:
            break
    return out


def note_from_event(ev: dict, *, base: str, actor: str, followers: str, mentions: dict,
                    in_reply_to: str = "", reply_to_actor: str = "", links: dict = None,
                    quote: str = "") -> dict:
    """A member's kind-1 as a public ActivityPub Note, addressed like Mastodon addresses one.

    `links` maps a referenced post's bech32 (note1/nevent1/naddr1) to its fediverse address;
    `quote` is the address of the post this one QUOTES -- sent in all three spellings servers read
    (`quoteUrl` Akkoma/Pleroma, `_misskey_quote` Misskey, `quoteUri` Fedibird), with the link
    left in the text for servers that read none of them."""
    att = attachments(ev)
    atts = [a["url"] for a in att]
    content = text_to_html(ev.get("content") or "", base=base, mentions=mentions, drop_urls=atts, links=links)
    cc = [followers] + sorted({m["href"] for m in mentions.values() if m.get("href")})
    if reply_to_actor and reply_to_actor not in cc:
        cc.append(reply_to_actor)
    tag = [{"type": "Mention", "href": m["href"], "name": m.get("name") or m["href"]}
           for m in mentions.values() if m.get("href")]
    tag += [{"type": "Hashtag", "href": f"{base}/tags/{h}", "name": f"#{h}"} for h in hashtags(ev)]
    tag += emoji_tags(ev)
    note = {
        "id": object_url(base, ev["id"]),
        "type": "Note",
        "attributedTo": actor,
        "content": content,
        "published": iso(ev.get("created_at")),
        "to": [PUBLIC],
        "cc": cc,
        "url": f"{base}/{_nevent_or_note(ev['id'])}",
        "tag": tag,
        "attachment": att,
        "sensitive": False,
    }
    if quote:
        note["quoteUrl"] = note["_misskey_quote"] = note["quoteUri"] = quote
    cw = next((t for t in ev.get("tags") or [] if t and t[0] == "content-warning"), None)
    if cw is not None:
        note["sensitive"] = True
        note["summary"] = (cw[1] if len(cw) > 1 else "") or "Sensitive content"
    if in_reply_to:
        note["inReplyTo"] = in_reply_to
    return note


def _nevent_or_note(event_id: str) -> str:
    """`note1…` -- the address this node's web client routes (a bare hex id is not a route)."""
    try:
        from app.services.nostr import bech32
        return bech32.encode("note", bytes.fromhex(event_id))
    except Exception:
        return event_id


def create(note: dict, actor: str) -> dict:
    return {"@context": AS_CONTEXT, "id": note["id"] + "/activity", "type": "Create", "actor": actor,
            "published": note.get("published"), "to": note.get("to"), "cc": note.get("cc"),
            "object": note}


def like(ev: dict, *, base: str, actor: str, target: str) -> dict:
    """A reaction. "+" is a plain Like; an emoji is a Like CARRYING it, the one shape every server
    understands: Mastodon counts a favourite, while Pleroma, Akkoma and Misskey show the emoji
    reaction -- they read `content` and Misskey's `_misskey_reaction`. A custom emoji rides as an
    `Emoji` tag with its image, or it arrives as bare `:shortcode:` text."""
    content = (ev.get("content") or "+").strip()
    act = {"@context": AS_CONTEXT, "id": activity_url(base, ev["id"]), "type": "Like",
           "actor": actor, "object": target}
    if content not in ("+", ""):
        act["content"] = content[:64]
        act["_misskey_reaction"] = content[:64]
        emo = emoji_tags(ev, content)
        if emo:
            act["tag"] = emo
    return act


def announce(ev: dict, *, base: str, actor: str, followers: str, target: str, target_actor: str = "") -> dict:
    cc = [followers] + ([target_actor] if target_actor else [])
    return {"@context": AS_CONTEXT, "id": activity_url(base, ev["id"]), "type": "Announce",
            "actor": actor, "published": iso(ev.get("created_at")), "to": [PUBLIC], "cc": cc,
            "object": target}


def delete_note(ev_id: str, *, base: str, actor: str, followers: str, deletion_id: str) -> dict:
    return {"@context": AS_CONTEXT, "id": activity_url(base, deletion_id, "delete"), "type": "Delete",
            "actor": actor, "to": [PUBLIC], "cc": [followers],
            "object": {"id": object_url(base, ev_id), "type": "Tombstone"}}


def undo(inner_id: str, inner_type: str, *, base: str, actor: str, target: str, deletion_id: str) -> dict:
    """Undo of a Like/Announce. `target` is what the inner activity was ABOUT, and it is not optional
    in practice: Mastodon and Misskey find the favourite to remove through it, so an Undo naming ""
    reached them and removed nothing."""
    return {"@context": AS_CONTEXT, "id": activity_url(base, deletion_id, "undo"), "type": "Undo",
            "actor": actor, "object": {"id": inner_id, "type": inner_type, "actor": actor, "object": target}}


# ------------------------------------------------------------------------------------ polls (NIP-88)

def poll_options(ev: dict) -> list:
    """[(option id, label)] of a kind-1068 poll, in order."""
    out, seen = [], set()
    for t in ev.get("tags") or []:
        if len(t) > 1 and t[0] == "option" and t[1] and t[1] not in seen:
            seen.add(t[1])
            out.append((str(t[1]), str(t[2] if len(t) > 2 and t[2] else t[1])[:200]))
    return out[:20]


def poll_multi(ev: dict) -> bool:
    return any(len(t) > 1 and t[0] == "polltype" and t[1] == "multiplechoice" for t in ev.get("tags") or [])


def poll_end(ev: dict) -> int:
    for t in ev.get("tags") or []:
        if len(t) > 1 and t[0] == "endsAt":
            try:
                return int(t[1])
            except (TypeError, ValueError):
                return 0
    return 0


def question_from_event(ev: dict, *, base: str, actor: str, followers: str, mentions: dict,
                        counts: dict = None, voters: int = 0, now: int = 0, **kw) -> dict:
    """A member's NIP-88 poll (kind 1068) as a Question -- what Mastodon, Pleroma, Akkoma and Misskey
    draw as a poll and let their users vote in. `counts` is {option id: votes} as tallied here."""
    note = note_from_event(ev, base=base, actor=actor, followers=followers, mentions=mentions, **kw)
    note["type"] = "Question"
    counts = counts or {}
    choices = [{"type": "Note", "name": label, "replies": {"type": "Collection", "totalItems": int(counts.get(oid, 0))}}
               for oid, label in poll_options(ev)]
    note["anyOf" if poll_multi(ev) else "oneOf"] = choices
    note["votersCount"] = int(voters)
    end = poll_end(ev)
    if end:
        note["endTime"] = iso(end)
        if end <= (now or int(datetime.now(timezone.utc).timestamp())):
            note["closed"] = iso(end)
    return note


def vote_note(*, vote_id: str, actor: str, question: str, question_actor: str, label: str) -> dict:
    """One answer to a fediverse poll, the way every server sends one: a Note carrying the option's
    NAME and no content, in reply to the Question, addressed to its author only."""
    return {"@context": AS_CONTEXT, "id": vote_id, "type": "Note", "attributedTo": actor, "name": label,
            "inReplyTo": question, "to": [question_actor], "cc": []}


def is_vote(note: dict) -> bool:
    """A poll answer: a Note with a `name`, no body, in reply to something."""
    return (isinstance(note, dict) and note.get("type") == "Note" and bool(str(note.get("name") or "").strip())
            and not str(note.get("content") or "").strip() and bool(id_of(note.get("inReplyTo"))))


def question_poll(note: dict) -> tuple[list, bool, int]:
    """([(option id, label)], multiple choice?, end time) of an incoming Question."""
    from app.services.fedi_normalize import _strip_html
    multi = bool(note.get("anyOf")) and not note.get("oneOf")
    opts = []
    for i, o in enumerate(_as_list(note.get("anyOf") if multi else note.get("oneOf"))):
        if isinstance(o, dict):
            label = _strip_html(str(o.get("name") or "")).strip()[:200]
            if label:
                opts.append((f"opt{i + 1}", label))
    end = parse_time(note.get("endTime") or note.get("closed"))
    return opts[:20], multi, end


def icon_url(v) -> str:
    """The https URL of an image reference in any shape ActivityStreams allows: a string, a Link or
    Image object (whose `url` may itself be a string, a Link or a list), or a list of those."""
    for _ in range(4):
        if isinstance(v, list):
            v = v[0] if v else None
        elif isinstance(v, dict):
            v = v.get("url") if v.get("url") is not None else v.get("href")
        else:
            break
    u = v.strip() if isinstance(v, str) else ""
    return u if u.startswith("https://") and len(u) < 2048 else ""


def person(*, base: str, name: str, profile: dict, public_key_pem: str, username: str = "",
           consented: bool = True) -> dict:
    """A member's actor document, filled from their kind-0 (`profile`, already parsed). `name` is the
    address (/ap/users/<name>); `username` the handle it SHOWS, when that differs (a Nostr user's
    readable handle -- see actors.readable_handle)."""
    actor = actor_url(base, name)
    doc = {
        "@context": AS_CONTEXT,
        "id": actor, "type": "Person",
        "preferredUsername": username or name,
        "name": (profile.get("display_name") or profile.get("name") or name)[:100],
        "summary": text_to_html(profile.get("about") or "", base=base, mentions={}),
        "url": f"{base}/users/{name}",
        "inbox": f"{actor}/inbox", "outbox": f"{actor}/outbox",
        "followers": f"{actor}/followers", "following": f"{actor}/following",
        "endpoints": {"sharedInbox": f"{base}/ap/inbox"},
        # Search indexing is the account's to agree to: a local user joined this server, while a
        # Nostr user served in `everyone` mode never asked to be put in a fediverse search index.
        "manuallyApprovesFollowers": False, "discoverable": bool(consented), "indexable": bool(consented),
        "featured": f"{actor}/featured",
        "publicKey": {"id": f"{actor}#main-key", "owner": actor, "publicKeyPem": public_key_pem},
    }
    # Profile FIELDS (Mastodon's metadata table): what the kind-0 says about where else to find them.
    fields = []
    site = str(profile.get("website") or "").strip()
    if site.startswith("https://") and len(site) < 500:
        esc = html.escape(site, quote=True)
        fields.append(("Website", f'<a href="{esc}" rel="me nofollow noopener noreferrer" '
                                  f'target="_blank">{html.escape(site.split("://", 1)[1])}</a>'))
    for label, key in (("Lightning", "lud16"), ("NIP-05", "nip05")):
        v = str(profile.get(key) or "").strip()
        if v and len(v) < 200 and re.fullmatch(r"[\w.+-]+@[\w.-]+", v):
            fields.append((label, html.escape(v)))
    if fields:
        doc["attachment"] = [{"type": "PropertyValue", "name": n, "value": v} for n, v in fields]
    pic = (profile.get("picture") or "").strip()
    if pic.startswith("https://"):
        doc["icon"] = {"type": "Image", "url": pic, "mediaType": media_type_of(pic) or "image/jpeg"}
    banner = (profile.get("banner") or "").strip()
    if banner.startswith("https://"):
        doc["image"] = {"type": "Image", "url": banner, "mediaType": media_type_of(banner) or "image/jpeg"}
    return doc


# ------------------------------------------------------------------------------------ inbound

def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def id_of(v) -> str:
    """An ActivityPub reference is either a URI string or an object carrying `id`."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("id") or "")
    return ""


def is_public(obj: dict) -> bool:
    """Public or unlisted -- i.e. addressed to as:Public in `to` or `cc`. Followers-only and direct
    messages are NOT stored: a Nostr kind-1 is readable by anyone, so storing one would publish a
    private post."""
    aud = [id_of(x) for x in _as_list(obj.get("to")) + _as_list(obj.get("cc"))]
    return any(a in (PUBLIC, "as:Public", "Public") for a in aud)


def account_from_actor(actor: dict) -> dict:
    """An actor document in the shape the bridge's identity code reads (a Mastodon account), so an
    author gets EXACTLY the puppet the Pleroma timeline bridge would give them."""
    from urllib.parse import urlparse
    aid = id_of(actor)
    host = urlparse(aid).hostname or ""
    user = username_of(actor)
    emojis = [{"shortcode": str(t.get("name") or "").strip(":"), "url": icon_url(t.get("icon"))}
              for t in _as_list(actor.get("tag")) if isinstance(t, dict) and t.get("type") == "Emoji"]
    return {"uri": aid, "url": aid, "acct": f"{user}@{host}" if user and host else "",
            "username": user, "display_name": str(actor.get("name") or user or "").strip(),
            "avatar": icon_url(actor.get("icon")),
            "note": str(actor.get("summary") or ""), "emojis": emojis}


def username_of(actor: dict) -> str:
    """An actor's preferredUsername, or "" when it could not be a handle. It is the SENDER'S text,
    and `bob@x` in it made `bob@x@evil.com` -- a handle no `bob@evil.com` block line matches."""
    user = str(actor.get("preferredUsername") or "").strip()
    return user if re.fullmatch(r"[^\s@/:#?]{1,100}", user) else ""


def note_content(note: dict, *, local_actors: dict) -> tuple[str, list]:
    """(text, tags) for an incoming Note: HTML flattened to text, attachments appended as links,
    mentions of OUR members turned into `p` tags (so they are notified), hashtags into `t` tags,
    and a content warning into NIP-36."""
    from app.services.fedi_normalize import _strip_html
    text = _strip_html(str(note.get("content") or ""))
    kind = note.get("type")
    title = _strip_html(str(note.get("name") or "")).strip()[:300] if kind in ("Article", "Page") else ""
    if title and not text.startswith(title):
        # An Article's `name` is its TITLE; dropped, a blog post arrived as a body with no heading.
        text = f"{title}\n\n{text}".strip()
    if kind == "Question":
        # A poll's choices live in oneOf/anyOf, not in the content: without them it was a question
        # with no answers. (Voting stays on the poll's own server -- the link below goes there.)
        opts = [_strip_html(str(o.get("name") or "")).strip()[:200]
                for o in _as_list(note.get("oneOf") or note.get("anyOf")) if isinstance(o, dict)]
        opts = [o for o in opts if o][:20]
        if opts:
            text = (text + "\n\n" + "\n".join(f"◯ {o}" for o in opts)).strip()
    urls, imeta = [], []
    for a in _as_list(note.get("attachment")):
        if isinstance(a, dict):
            u = a.get("url")
            if isinstance(u, list):
                u = next((id_of(x) or (x.get("href") if isinstance(x, dict) else "") for x in u), "")
            u = id_of(u) if not isinstance(u, str) else u
            if isinstance(u, str) and u.startswith("https://") and u not in text:
                urls.append(u)
                # NIP-92: what the attachment IS, so a Nostr client can size it before it loads,
                # blur it while it does, and read its description out.
                meta = [f"url {u}"]
                mt = str(a.get("mediaType") or "")
                if re.fullmatch(r"(image|video|audio)/[\w.+-]{1,40}", mt):
                    meta.append(f"m {mt}")
                if isinstance(a.get("width"), int) and isinstance(a.get("height"), int):
                    meta.append(f"dim {a['width']}x{a['height']}")
                if re.fullmatch(r"[0-9A-Za-z#$%*+,\-.:;=?@\[\]^_{|}~]{6,100}", str(a.get("blurhash") or "")):
                    meta.append(f"blurhash {a['blurhash']}")
                alt = " ".join(str(a.get("name") or "").split())[:1500]
                if alt:
                    meta.append(f"alt {alt}")
                if len(meta) > 1:
                    imeta.append(["imeta"] + meta)
    if urls:
        text = (text + "\n\n" + "\n".join(urls)).strip()
    quote = next((q for q in (id_of(note.get(k)) for k in ("quoteUrl", "_misskey_quote", "quoteUri", "quote"))
                  if isinstance(q, str) and q.startswith("https://")), "")
    if quote and quote not in text:
        text = (text + "\n\n" + quote).strip()   # the quoted post, as a link every client renders
    tags, seen_p = list(imeta[:8]), set()      # p tags are bounded by who is OURS; t tags are capped below
    for t in _as_list(note.get("tag")):
        if not isinstance(t, dict):
            continue
        if t.get("type") == "Mention":
            pk = local_actors.get(id_of(t.get("href")))
            if pk and pk not in seen_p:
                seen_p.add(pk)
                tags.append(["p", pk])
        elif t.get("type") == "Hashtag":
            name = str(t.get("name") or "").lstrip("#").strip().lower()
            if name and len(name) <= 64 and sum(1 for x in tags if x[0] == "t") < 20:
                tags.append(["t", name])
    if kind in ("Article", "Page"):
        if note.get("sensitive"):
            tags.append(["content-warning", ""])   # an Article's `summary` is its abstract, not a CW
    elif note.get("sensitive") or note.get("summary"):
        tags.append(["content-warning", str(note.get("summary") or "")[:200]])
    return text, tags
