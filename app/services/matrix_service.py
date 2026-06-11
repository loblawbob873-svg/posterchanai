"""Matrix Client-Server API helpers."""

import logging
import time
import uuid
import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0

# 5xx worth backing off and retrying in-request (a brief overload may clear within a few retries).
_TRANSIENT_STATUSES = (500, 502, 503, 504)
# A subset that means "the server never processed the request" (overloaded/down / proxy timeout) —
# infra, not this payload. Surfaced as MatrixServerError so callers retry the work LATER (e.g. hold
# the timeline cursor). 500 is deliberately excluded: it usually means the server errored on THIS
# event, so treating it as retry-later would wedge a poller forever on one poison payload.
_UNAVAILABLE_STATUSES = (502, 503, 504)


class MatrixServerError(Exception):
    """The homeserver was unavailable (502/503/504) after retries. Callers should retry the
    operation later rather than skipping it permanently."""


def _txn_id() -> str:
    """Unique transaction id. uuid (not the millisecond clock) so a burst of sends in the
    same loop iteration can't collide and get deduplicated by the homeserver."""
    return uuid.uuid4().hex


async def _with_429_retry(make_request, attempts: int = 4):
    """Call make_request() (an async fn returning a Response); on HTTP 429 honour the
    homeserver's retry_after_ms and retry. Retrying the same txn id is idempotent on Matrix,
    so this safely rides out the rate limits a busy feed bridge hits.

    NB: 5xx is deliberately NOT retried in-request. A 504 is a reverse-proxy gateway timeout —
    each attempt already waited the full upstream timeout (tens of seconds), so retrying it 4×
    would stall a poll past its cap and wedge the bridge. We fail fast and let the caller defer
    the work to the next poll cycle (that cadence IS the backoff)."""
    import asyncio
    resp = None
    for _ in range(attempts):
        resp = await make_request()
        if resp.status_code != 429:
            return resp
        retry_ms = 1000
        try:
            retry_ms = int(resp.json().get("retry_after_ms", 1000))
        except Exception:
            pass
        await asyncio.sleep(min(max(retry_ms, 100), 5000) / 1000.0)
    return resp


def _send_error(label: str, status: int, text: str) -> Exception:
    """Pick the right exception for a non-2xx Matrix response: server-unavailable (502/503/504) →
    MatrixServerError so callers retry later; everything else (incl. 500 — likely payload-specific)
    → ValueError, which callers log and skip rather than retrying forever."""
    msg = f"{label}: HTTP {status} — {text}"
    return MatrixServerError(msg) if status in _UNAVAILABLE_STATUSES else ValueError(msg)


async def request(homeserver: str, access_token: str, method: str, endpoint: str,
                  data: dict | None = None, params: dict | None = None,
                  verify_ssl: bool = True, timeout: float = 60.0):
    """Generic Matrix Client-Server API call against the r0 endpoint:
    {homeserver}/_matrix/client/r0/{endpoint}. Returns parsed JSON on 200/201, else None.

    Mirrors the standalone bot client's matrix_request so botframework/matrix_shim.py can route
    every call through this shared service. (timeout defaults to 60s so a /sync long-poll —
    server-side timeout up to 30s — isn't cut off by the client.)"""
    url = homeserver.rstrip("/") + f"/_matrix/client/r0/{endpoint}"
    headers = {"Authorization": f"Bearer {access_token}"}
    method = method.upper()
    async with httpx.AsyncClient(timeout=timeout, verify=verify_ssl) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers, params=params)
        elif method == "POST":
            resp = await client.post(url, headers=headers, json=data, params=params)
        elif method == "PUT":
            resp = await client.put(url, headers=headers, json=data, params=params)
        else:
            return None
        if resp.status_code in (200, 201):
            try:
                return resp.json()
            except Exception:
                return None
        return None


def render_matrix_html(text: str) -> str:
    """Render Markdown/plain text to the limited HTML Matrix (Element) shows.

    A plain `body` is displayed verbatim, but as soon as a message carries a
    `formatted_body` the client renders the HTML — where newlines collapse to
    spaces. Auto-linking URLs without also turning newlines into <br> is what
    made multi-line posts appear "mashed together". This converts the common
    inline Markdown, auto-links bare URLs, and maps every newline to <br>.
    """
    import re as _re
    import html as _html

    escaped = _html.escape(text, quote=False)

    def inline(s: str) -> str:
        # Inline code: `code`
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # Markdown links: [text](url)
        s = _re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        # Bold / italic
        s = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
        s = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        s = _re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", s)
        # Auto-link bare URLs not already inside an anchor (href=" or >URL<)
        s = _re.sub(r'(?<!href=")(?<!>)(https?://[^\s<]+)', r'<a href="\1">\1</a>', s)
        return s

    lines = []
    for line in escaped.split("\n"):
        m = _re.match(r"^\s*(#{1,6})\s+(.*)$", line)
        if m:
            lines.append(f"<strong>{inline(m.group(2))}</strong>")
        else:
            lines.append(inline(line))

    return "<br>".join(lines)


async def login(homeserver: str, username: str, password: str) -> dict:
    """Log in to a Matrix homeserver with username/password.

    Returns {"access_token": str, "user_id": str} or raises an exception.
    """
    hs = homeserver.rstrip("/")
    url = f"{hs}/_matrix/client/v3/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code in (401, 403):
            raise ValueError("Invalid username or password")
        if resp.status_code != 200:
            raise ValueError(f"Login failed: HTTP {resp.status_code} — {resp.text[:200]}")
        data = resp.json()
        return {"access_token": data["access_token"], "user_id": data["user_id"]}


async def logout(homeserver: str, access_token: str) -> None:
    """Log out from a Matrix homeserver (invalidates access_token)."""
    hs = homeserver.rstrip("/")
    url = f"{hs}/_matrix/client/v3/logout"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            await client.post(url, headers=headers)
        except Exception as e:
            logger.warning(f"Matrix logout error (ignored): {e}")


async def get_joined_rooms(homeserver: str, access_token: str) -> list[dict]:
    """Return a list of joined rooms as [{"room_id": str, "name": str}, ...].

    Room names are fetched from the m.room.name state event; if unavailable the
    room ID is used as the display name.
    """
    hs = homeserver.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{hs}/_matrix/client/v3/joined_rooms", headers=headers)
        if resp.status_code != 200:
            raise ValueError(f"Failed to list rooms: HTTP {resp.status_code}")
        room_ids: list[str] = resp.json().get("joined_rooms", [])

        import asyncio
        capped = room_ids[:30]
        names = await asyncio.gather(
            *[_room_display_name(client, hs, rid, headers) for rid in capped]
        )
        rooms = [{"room_id": rid, "name": name} for rid, name in zip(capped, names)]
    return rooms


async def _room_display_name(client: httpx.AsyncClient, hs: str, room_id: str, headers: dict) -> str:
    """Try to get the human-readable room name; fall back to room_id."""
    try:
        from urllib.parse import quote
        encoded = quote(room_id, safe="")
        url = f"{hs}/_matrix/client/v3/rooms/{encoded}/state/m.room.name"
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            name = r.json().get("name", "").strip()
            if name:
                return name
    except Exception:
        pass
    return room_id


async def _get_dm_room_ids(client: httpx.AsyncClient, hs: str, headers: dict, own_user_id: str) -> set:
    """Room IDs the user has tagged as direct messages, from the m.direct account data
    (a map of other-user -> [room_ids]). Returns an empty set if unset/unavailable."""
    try:
        from urllib.parse import quote
        uid = quote(own_user_id, safe="")
        r = await client.get(f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.direct", headers=headers)
        if r.status_code != 200:
            return set()
        rooms: set = set()
        for room_list in (r.json() or {}).values():
            if isinstance(room_list, list):
                rooms.update(room_list)
        return rooms
    except Exception as e:
        logger.debug(f"[matrix] could not fetch m.direct: {e}")
        return set()


def _mentions_user(content: dict, own_user_id: str) -> bool:
    """True if a message event mentions own_user_id. Covers modern intentional mentions
    (m.mentions, MSC3952) and the mention pill / raw mxid in the HTML or plain body."""
    mentions = content.get("m.mentions") or {}
    if own_user_id in (mentions.get("user_ids") or []):
        return True
    formatted = content.get("formatted_body") or ""   # mention pills embed the mxid here
    body = content.get("body") or ""
    return own_user_id in formatted or own_user_id in body


async def fetch_notifications(homeserver: str, access_token: str, own_user_id: str,
                              since: str | None = None) -> tuple[list[dict], str]:
    """Incremental sync for new room messages worth notifying about. Returns (events,
    next_batch).

    Each event is {room_id, event_id, sender, body}. Forwards m.room.message events from
    other users when the room is a DM (every message) or, in group rooms, only when the
    message mentions own_user_id. On the FIRST poll (since is None) we only capture the
    current next_batch cursor and return no events — this avoids replaying the whole history
    when the relay is first enabled."""
    import json as _json
    hs = homeserver.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    sync_filter = _json.dumps({
        # m.room.encrypted is included so we can notify about (undecryptable) DMs.
        "room": {"timeline": {"types": ["m.room.message", "m.room.encrypted"], "limit": 30}},
        "presence": {"types": []},
        "account_data": {"types": []},
    })
    params = {"timeout": "0", "filter": sync_filter}
    if since:
        params["since"] = since
    url = f"{hs}/_matrix/client/v3/sync"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            raise ValueError(f"Matrix sync failed: HTTP {resp.status_code} — {resp.text[:200]}")
        data = resp.json()
        next_batch = data.get("next_batch", since or "")
        if not since:
            return [], next_batch  # first poll: establish cursor, no backfill
        dm_rooms = await _get_dm_room_ids(client, hs, headers, own_user_id)
    events: list[dict] = []
    encrypted_dm_rooms: set = set()  # DM rooms with new (undecryptable) messages → one notice each
    for room_id, room in (data.get("rooms", {}).get("join", {}) or {}).items():
        is_dm = room_id in dm_rooms
        for ev in room.get("timeline", {}).get("events", []) or []:
            etype = ev.get("type")
            if etype not in ("m.room.message", "m.room.encrypted"):
                continue
            sender = ev.get("sender", "")
            if sender == own_user_id:
                continue
            if etype == "m.room.encrypted":
                # Can't decrypt; only flag DMs so the user knows to check Element.
                if is_dm:
                    encrypted_dm_rooms.add(room_id)
                continue
            content = ev.get("content", {}) or {}
            # DM rooms: every incoming message. Group rooms: mentions only.
            if not (is_dm or _mentions_user(content, own_user_id)):
                continue
            events.append({
                "room_id": room_id,
                "event_id": ev.get("event_id", ""),
                "sender": sender,
                "body": content.get("body", ""),
                "encrypted": False,
            })
    # One "you have an encrypted DM" notice per room (collapses a burst into a single ping).
    for room_id in encrypted_dm_rooms:
        events.append({"room_id": room_id, "event_id": "", "sender": "", "body": "", "encrypted": True})
    return events, next_batch


async def send_message(homeserver: str, access_token: str, room_id: str, text: str) -> None:
    """Send a message to a Matrix room with URL auto-linking."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    encoded_room = quote(room_id, safe="")
    txn_id = str(int(time.time() * 1000))
    url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    # Build formatted_body so links render AND newlines are preserved as <br>;
    # without the <br> conversion multi-line posts collapse onto one line.
    import html as _html
    formatted = render_matrix_html(text)
    payload: dict = {"msgtype": "m.text", "body": text}
    if formatted != _html.escape(text, quote=False):
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = formatted
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_429_retry(lambda: client.put(url, json=payload, headers=headers))
        if resp.status_code not in (200, 201):
            raise _send_error("Failed to send Matrix message", resp.status_code, resp.text[:200])


async def send_event(homeserver: str, access_token: str, room_id: str, body: str,
                     html: str | None = None, thread_root_event_id: str | None = None,
                     reply_to_event_id: str | None = None, as_thread: bool = True) -> str:
    """Send a text message and return its event_id.

    Like send_message but: returns the new event_id (needed to thread replies under it),
    accepts pre-rendered `html` (falls back to render_matrix_html(body)), and threads the
    message under thread_root_event_id when given. `reply_to_event_id`, when provided, is the
    actual parent event the message replies to (so Element shows the real reply chain inside the
    thread); otherwise the thread root is used as the fallback in-reply-to."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    import html as _html
    encoded_room = quote(room_id, safe="")
    url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{_txn_id()}"
    headers = {"Authorization": f"Bearer {access_token}"}
    formatted = html if html is not None else render_matrix_html(body)
    payload: dict = {"msgtype": "m.text", "body": body}
    if formatted != _html.escape(body, quote=False):
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = formatted
    if thread_root_event_id or reply_to_event_id:
        in_reply = reply_to_event_id or thread_root_event_id
        if as_thread and thread_root_event_id:
            # A real parent → a genuine threaded reply (is_falling_back False); otherwise the root
            # is just the thread's fallback in-reply-to.
            payload["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_root_event_id,
                "is_falling_back": reply_to_event_id is None,
                "m.in_reply_to": {"event_id": in_reply},
            }
        else:
            # Inline rich reply (no m.thread): Element renders it in the MAIN timeline with a
            # clickable quote of the parent, instead of hiding it in a thread pane.
            payload["m.relates_to"] = {"m.in_reply_to": {"event_id": in_reply}}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_429_retry(lambda: client.put(url, json=payload, headers=headers))
        if resp.status_code not in (200, 201):
            raise _send_error("Failed to send Matrix event", resp.status_code, resp.text[:200])
        return resp.json().get("event_id", "")


def _detect_mime(image_bytes: bytes) -> tuple[str, str]:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image.jpg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image.png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image.gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    if image_bytes[4:8] == b"ftyp":
        return "video/mp4", "video.mp4"
    if image_bytes[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", "video.webm"
    return "image/jpeg", "image.jpg"


async def upload_media_bytes(homeserver: str, access_token: str, data: bytes,
                             mime: str, filename: str = "file") -> str:
    """Upload raw bytes to the Matrix media repo and return the mxc:// URI.

    Tries the v1 endpoint first, falling back to the legacy v3 path."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": mime}
    fname = quote(filename, safe="")
    last_status, last_text = 0, ""
    # 30s (not 120s): a healthy upload is fast; if Synapse is wedged we want to fail fast and
    # defer to the next poll cycle rather than pin a poll slot waiting on a stalled homeserver.
    async with httpx.AsyncClient(timeout=30.0) as client:
        for media_url in [
            f"{hs}/_matrix/client/v1/media/upload?filename={fname}",
            f"{hs}/_matrix/media/v3/upload?filename={fname}",
        ]:
            resp = await _with_429_retry(lambda: client.post(media_url, content=data, headers=headers))
            if resp.status_code in (200, 201):
                mxc = resp.json().get("content_uri")
                if mxc:
                    return mxc
            last_status, last_text = resp.status_code, resp.text[:200]
            # The v1→v3 fallback is for "endpoint unsupported" (4xx); if the server is unavailable,
            # the alt endpoint is the same server — don't double the backoff probing it.
            if resp.status_code in _UNAVAILABLE_STATUSES:
                break
    raise _send_error("Media upload failed", last_status, last_text)


_MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
             "video/mp4": "mp4", "video/webm": "webm"}


async def send_media_event(homeserver: str, access_token: str, room_id: str, mxc_uri: str,
                           mime: str, caption: str = "", caption_html: str | None = None,
                           w: int | None = None, h: int | None = None, size: int | None = None,
                           filename: str | None = None, thread_root_event_id: str | None = None,
                           reply_to_event_id: str | None = None, as_thread: bool = True) -> str:
    """Send an m.image/m.video event referencing an ALREADY-uploaded mxc, so cached media isn't
    re-uploaded (and Synapse doesn't store a duplicate blob). send_image == upload + this.
    Caption/threading semantics match send_image."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    is_video = mime.startswith("video/")
    fname = filename or f"file.{_MIME_EXT.get(mime, 'bin')}"
    headers_auth = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        encoded_room = quote(room_id, safe="")
        send_url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{_txn_id()}"
        info: dict = {"mimetype": mime}
        if size is not None:
            info["size"] = size
        # Element needs w/h to SIZE the media inline — for BOTH image AND video. Without w/h a video
        # renders cut off / wrong-sized in the timeline (the long-standing "video size cut off" bug).
        if w and h:
            info["w"], info["h"] = w, h
        payload: dict = {
            "msgtype": "m.video" if is_video else "m.image",
            "url": mxc_uri,
            "info": info,
        }
        if caption:
            # Media caption (MSC2530): body is the caption, filename is the real file name.
            payload["body"] = caption
            payload["filename"] = fname
            if caption_html:
                payload["format"] = "org.matrix.custom.html"
                payload["formatted_body"] = caption_html
        else:
            payload["body"] = fname
        if thread_root_event_id or reply_to_event_id:
            in_reply = reply_to_event_id or thread_root_event_id
            if as_thread and thread_root_event_id:
                payload["m.relates_to"] = {
                    "rel_type": "m.thread",
                    "event_id": thread_root_event_id,
                    "is_falling_back": reply_to_event_id is None,
                    "m.in_reply_to": {"event_id": in_reply},
                }
            else:
                # Inline rich reply (no m.thread) — renders in the main timeline.
                payload["m.relates_to"] = {"m.in_reply_to": {"event_id": in_reply}}
        resp = await _with_429_retry(lambda: client.put(send_url, json=payload, headers=headers_auth))
        if resp.status_code not in (200, 201):
            raise _send_error("Failed to send Matrix image", resp.status_code, resp.text[:200])
        return resp.json().get("event_id", "")


async def send_image(homeserver: str, access_token: str, room_id: str,
                     image_bytes: bytes, caption: str = "", mime: str = "",
                     thread_root_event_id: str | None = None, caption_html: str | None = None,
                     reply_to_event_id: str | None = None, as_thread: bool = True) -> str:
    """Upload image/video bytes to Matrix media and send it in a room (m.image, or m.video when
    the bytes are a video). When `caption` is given it's embedded in the media event as a media
    caption (MSC2530: body=caption, filename=real name, optional formatted_body=caption_html), so
    text+image render as ONE message. Threads under thread_root_event_id (with reply_to_event_id
    as the actual parent). Returns the media event's event_id."""
    detected_mime, filename = _detect_mime(image_bytes)
    # Trust the byte sniff for video (callers may pass an image/* default mime).
    mime = detected_mime if detected_mime.startswith("video/") else (mime or detected_mime)
    is_video = mime.startswith("video/")
    mxc_uri = await upload_media_bytes(homeserver, access_token, image_bytes, mime, filename)
    # Pixel dimensions — Element needs w/h to render an image inline (PIL can't open video).
    w = h = None
    if not is_video:
        try:
            from PIL import Image as _PILImage
            from io import BytesIO as _BytesIO
            with _PILImage.open(_BytesIO(image_bytes)) as _im:
                w, h = _im.width, _im.height
        except Exception as _dim_err:
            logger.debug(f"Could not read image dimensions: {_dim_err}")
    else:
        # PIL can't open video, so ffprobe the stream w/h — Element needs them to size the video
        # (without them it renders cut off). Mirrors the bot client's matrix_client.py probe.
        try:
            import tempfile as _tf, subprocess as _sp, os as _os
            _fd, _vp = _tf.mkstemp(suffix=".mp4"); _os.close(_fd)
            try:
                with open(_vp, "wb") as _f:
                    _f.write(image_bytes)
                _r = _sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", _vp],
                             capture_output=True, text=True, timeout=30)
                _d = (_r.stdout or "").strip().split("x")
                if len(_d) == 2 and _d[0].isdigit() and _d[1].isdigit():
                    w, h = int(_d[0]), int(_d[1])
            finally:
                _os.unlink(_vp)
        except Exception as _ve:
            logger.debug(f"Could not read video dimensions: {_ve}")
    return await send_media_event(
        homeserver, access_token, room_id, mxc_uri, mime, caption=caption,
        caption_html=caption_html, w=w, h=h, size=len(image_bytes), filename=filename,
        thread_root_event_id=thread_root_event_id, reply_to_event_id=reply_to_event_id,
        as_thread=as_thread)


async def _ensure_joined(client: httpx.AsyncClient, hs: str, room_id: str, headers: dict) -> bool:
    """Join a room if not already a member. Returns True if joined/already joined."""
    from urllib.parse import quote
    encoded = quote(room_id, safe="")
    r = await client.post(f"{hs}/_matrix/client/v3/join/{encoded}", json={}, headers=headers)
    return r.status_code in (200, 201)


async def _is_encrypted(client: httpx.AsyncClient, hs: str, room_id: str, headers: dict) -> bool:
    """Return True only if the room definitively has m.room.encryption (HTTP 200).

    A 403/404 means the state is unreadable (often just 'not joined') — NOT proof
    of encryption. Treating those as encrypted caused duplicate rooms on transient
    errors; instead let _ensure_joined attempt recovery on the existing room.
    """
    from urllib.parse import quote
    encoded = quote(room_id, safe="")
    r = await client.get(
        f"{hs}/_matrix/client/v3/rooms/{encoded}/state/m.room.encryption",
        headers=headers,
    )
    return r.status_code == 200


async def create_dm_room_with(homeserver: str, access_token: str, invite_mxid: str,
                              name: str = "Fediverse Notifications") -> str:
    """Create an unencrypted private room owned by the token's account and invite invite_mxid,
    returning the room id. Used by the bot to open a per-user notification DM. No is_direct /
    encryption so the bot's plain messages stay readable (mirrors create_or_get_dm_room)."""
    hs = homeserver.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{hs}/_matrix/client/v3/createRoom",
            json={"name": name, "preset": "private_chat", "invite": [invite_mxid]},
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to create DM room: HTTP {resp.status_code} — {resp.text[:200]}")
        return resp.json()["room_id"]


async def create_or_get_dm_room(homeserver: str, access_token: str, bot_user_id: str) -> str:
    """Return an unencrypted bot room ID, creating one if needed.

    Uses custom account data key `posterchanai.bot_room` to remember the room
    across calls. Skips any room that is E2EE encrypted (created a new one instead).
    Does NOT use is_direct so Element won't auto-encrypt it.
    """
    from urllib.parse import quote
    hs = homeserver.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    ACCOUNT_KEY = "posterchanai.bot_room"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Get own user_id
        whoami = await client.get(f"{hs}/_matrix/client/v3/account/whoami", headers=headers)
        user_id = whoami.json().get("user_id", "") if whoami.status_code == 200 else ""

        # Check custom account data for a previously created bot room
        if user_id:
            acct_r = await client.get(
                f"{hs}/_matrix/client/v3/user/{quote(user_id, safe='')}/account_data/{ACCOUNT_KEY}",
                headers=headers,
            )
            if acct_r.status_code == 200:
                saved_room = acct_r.json().get("room_id", "")
                if saved_room:
                    encrypted = await _is_encrypted(client, hs, saved_room, headers)
                    if not encrypted:
                        joined = await _ensure_joined(client, hs, saved_room, headers)
                        if joined:
                            logger.info(f"Reusing existing bot room {saved_room}")
                            return saved_room
                    logger.info(f"Saved bot room {saved_room} is encrypted or unjoinable, creating new one")

        # Fetch the bot's avatar so the room shows it
        bot_avatar_mxc = ""
        try:
            prof = await client.get(
                f"{hs}/_matrix/client/v3/profile/{quote(bot_user_id, safe='')}",
                headers=headers,
            )
            if prof.status_code == 200:
                bot_avatar_mxc = prof.json().get("avatar_url", "") or ""
        except Exception as e:
            logger.debug(f"Could not fetch bot avatar: {e}")

        initial_state = []
        if bot_avatar_mxc:
            initial_state.append({
                "type": "m.room.avatar",
                "state_key": "",
                "content": {"url": bot_avatar_mxc},
            })

        # Create a new private room without is_direct so clients don't auto-enable E2EE
        logger.info(f"Creating new unencrypted bot room with {bot_user_id}")
        resp = await client.post(
            f"{hs}/_matrix/client/v3/createRoom",
            json={
                "name": "Posterchanai Bot",
                "invite": [bot_user_id],
                "preset": "private_chat",
                "initial_state": initial_state,  # avatar; no m.room.encryption = unencrypted
            },
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to create bot room: HTTP {resp.status_code} — {resp.text[:200]}")
        room_id = resp.json()["room_id"]

        # Save room_id to account data so future calls reuse it
        if user_id:
            await client.put(
                f"{hs}/_matrix/client/v3/user/{quote(user_id, safe='')}/account_data/{ACCOUNT_KEY}",
                json={"room_id": room_id},
                headers=headers,
            )
        return room_id
