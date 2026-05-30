"""Matrix Client-Server API helpers."""

import logging
import time
import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


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
        resp = await client.put(url, json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to send Matrix message: HTTP {resp.status_code} — {resp.text[:200]}")


def _detect_mime(image_bytes: bytes) -> tuple[str, str]:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image.jpg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image.png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image.gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    return "image/jpeg", "image.jpg"


async def send_image(homeserver: str, access_token: str, room_id: str,
                     image_bytes: bytes, caption: str = "", mime: str = "") -> None:
    """Upload image bytes to Matrix media and send as m.image in a room."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    detected_mime, filename = _detect_mime(image_bytes)
    mime = mime or detected_mime
    headers_auth = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Upload media — try v1 endpoint first, fall back to legacy
        for media_url in [
            f"{hs}/_matrix/client/v1/media/upload?filename={filename}",
            f"{hs}/_matrix/media/v3/upload?filename={filename}",
        ]:
            upload_resp = await client.post(
                media_url,
                content=image_bytes,
                headers={**headers_auth, "Content-Type": mime},
            )
            if upload_resp.status_code in (200, 201):
                mxc_uri = upload_resp.json().get("content_uri")
                break
        else:
            raise ValueError(f"Media upload failed: HTTP {upload_resp.status_code} — {upload_resp.text[:200]}")

        if not mxc_uri:
            raise ValueError("Media upload returned no content_uri")

        encoded_room = quote(room_id, safe="")
        txn_id = str(int(time.time() * 1000))
        send_url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
        _info = {"mimetype": mime, "size": len(image_bytes)}
        # Pixel dimensions — Matrix clients (Element) need w/h to render inline
        # instead of showing a download attachment.
        try:
            from PIL import Image as _PILImage
            from io import BytesIO as _BytesIO
            with _PILImage.open(_BytesIO(image_bytes)) as _im:
                _info["w"], _info["h"] = _im.width, _im.height
        except Exception as _dim_err:
            logger.debug(f"Could not read image dimensions: {_dim_err}")
        payload = {
            "msgtype": "m.image",
            "body": filename,
            "url": mxc_uri,
            "info": _info,
        }
        # If there's a caption send it as a separate text message after the image
        resp = await client.put(send_url, json=payload, headers=headers_auth)
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to send Matrix image: HTTP {resp.status_code} — {resp.text[:200]}")

        # Send caption as a follow-up text message if provided
        if caption:
            txn_id2 = str(int(time.time() * 1000) + 1)
            send_url2 = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id2}"
            await client.put(send_url2, json={"msgtype": "m.text", "body": caption}, headers=headers_auth)


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
