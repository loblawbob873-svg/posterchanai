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
              {"Hashtag": "as:Hashtag", "sensitive": "as:sensitive"}]

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_NOSTR_REF_RE = re.compile(r"nostr:((?:npub1|nprofile1)[023456789acdefghjklmnpqrstuvwxyz]+)", re.I)
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


def text_to_html(text: str, *, base: str, mentions: dict, drop_urls=()) -> str:
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
                out.append(f'<span class="h-card"><a href="{html.escape(who["href"], quote=True)}" '
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


def note_from_event(ev: dict, *, base: str, actor: str, followers: str, mentions: dict,
                    in_reply_to: str = "", reply_to_actor: str = "") -> dict:
    """A member's kind-1 as a public ActivityPub Note, addressed like Mastodon addresses one."""
    atts = media_urls(ev)
    content = text_to_html(ev.get("content") or "", base=base, mentions=mentions, drop_urls=atts)
    cc = [followers] + sorted({m["href"] for m in mentions.values() if m.get("href")})
    if reply_to_actor and reply_to_actor not in cc:
        cc.append(reply_to_actor)
    tag = [{"type": "Mention", "href": m["href"], "name": m.get("name") or m["href"]}
           for m in mentions.values() if m.get("href")]
    tag += [{"type": "Hashtag", "href": f"{base}/tags/{h}", "name": f"#{h}"} for h in hashtags(ev)]
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
        "attachment": [{"type": "Document", "mediaType": media_type_of(u) or "application/octet-stream",
                        "url": u, "name": None} for u in atts],
        "sensitive": False,
    }
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
    content = (ev.get("content") or "+").strip()
    act = {"@context": AS_CONTEXT, "id": activity_url(base, ev["id"]), "type": "Like",
           "actor": actor, "object": target}
    if content not in ("+", ""):
        act["content"] = content[:32]          # an emoji reaction (Misskey/Pleroma read `content`)
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
    return {"@context": AS_CONTEXT, "id": activity_url(base, deletion_id, "undo"), "type": "Undo",
            "actor": actor, "object": {"id": inner_id, "type": inner_type, "actor": actor, "object": target}}


def person(*, base: str, name: str, profile: dict, public_key_pem: str) -> dict:
    """A member's actor document, filled from their kind-0 (`profile`, already parsed)."""
    actor = actor_url(base, name)
    doc = {
        "@context": AS_CONTEXT,
        "id": actor, "type": "Person",
        "preferredUsername": name,
        "name": (profile.get("display_name") or profile.get("name") or name)[:100],
        "summary": text_to_html(profile.get("about") or "", base=base, mentions={}),
        "url": f"{base}/users/{name}",
        "inbox": f"{actor}/inbox", "outbox": f"{actor}/outbox",
        "followers": f"{actor}/followers", "following": f"{actor}/following",
        "endpoints": {"sharedInbox": f"{base}/ap/inbox"},
        "manuallyApprovesFollowers": False, "discoverable": True,
        "publicKey": {"id": f"{actor}#main-key", "owner": actor, "publicKeyPem": public_key_pem},
    }
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
    user = str(actor.get("preferredUsername") or "").strip()
    icon = actor.get("icon")
    if isinstance(icon, list):
        icon = icon[0] if icon else None
    emojis = [{"shortcode": str(t.get("name") or "").strip(":"), "url": id_of((t.get("icon") or {}).get("url"))
               or str((t.get("icon") or {}).get("url") or "")}
              for t in _as_list(actor.get("tag")) if isinstance(t, dict) and t.get("type") == "Emoji"]
    return {"uri": aid, "url": aid, "acct": f"{user}@{host}" if user and host else "",
            "username": user, "display_name": str(actor.get("name") or user or "").strip(),
            "avatar": (icon or {}).get("url") if isinstance(icon, dict) else "",
            "note": str(actor.get("summary") or ""), "emojis": emojis}


def note_content(note: dict, *, local_actors: dict) -> tuple[str, list]:
    """(text, tags) for an incoming Note: HTML flattened to text, attachments appended as links,
    mentions of OUR members turned into `p` tags (so they are notified), hashtags into `t` tags,
    and a content warning into NIP-36."""
    from app.services.fedi_normalize import _strip_html
    text = _strip_html(str(note.get("content") or ""))
    urls = []
    for a in _as_list(note.get("attachment")):
        if isinstance(a, dict):
            u = a.get("url")
            if isinstance(u, list):
                u = next((id_of(x) or (x.get("href") if isinstance(x, dict) else "") for x in u), "")
            u = id_of(u) if not isinstance(u, str) else u
            if isinstance(u, str) and u.startswith("https://") and u not in text:
                urls.append(u)
    if urls:
        text = (text + "\n\n" + "\n".join(urls)).strip()
    tags, seen_p = [], set()      # p tags are bounded by who is OURS; t tags are capped below
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
    if note.get("sensitive") or note.get("summary"):
        tags.append(["content-warning", str(note.get("summary") or "")[:200]])
    return text, tags
