# matrix_client.py
import json
import re
import html as _html
import requests
import uuid
from urllib.parse import quote
from config import MATRIX_SERVER, MATRIX_ACCESS_TOKEN, MATRIX_USER_ID, MATRIX_ADMINS, BLOCK_PHRASE, MATRIX_VERIFY_SSL


def markdown_to_matrix_html(text):
    """Render Markdown to the limited HTML Matrix (Element) understands.

    Matrix only renders rich text when a message carries a `formatted_body`
    with format `org.matrix.custom.html`; a plain `body` is shown verbatim,
    so Markdown links/bold and — crucially — single newlines collapse and the
    message looks "bunched together". We convert the common inline Markdown and
    turn every newline into <br> so each line stays on its own line.
    """
    if not text:
        return ""

    # Escape first so content can't inject markup. Markdown punctuation
    # ([](){}#*_`-) survives escaping, so it can still be parsed afterwards.
    escaped = _html.escape(text, quote=False)

    def inline(s):
        # Inline code: `code`
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # Links: [text](url)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        # Bold: **text** / __text__
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
        # Italic: *text* / _text_ (single delimiters, not touching bold)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", s)
        return s

    out_lines = []
    for line in escaped.split("\n"):
        # Headers (# .. ######) -> bold line so they don't show literal '#'.
        m = re.match(r"^\s*(#{1,6})\s+(.*)$", line)
        if m:
            out_lines.append(f"<strong>{inline(m.group(2))}</strong>")
        else:
            out_lines.append(inline(line))

    return "<br>".join(out_lines)

matrix_server = MATRIX_SERVER.rstrip('/') if MATRIX_SERVER else None
matrix_token = MATRIX_ACCESS_TOKEN
matrix_verify_ssl = MATRIX_VERIFY_SSL
matrix_headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {matrix_token}"
}

def matrix_request(method, endpoint, data=None, params=None):
    if not matrix_server:
        print("ERROR: MATRIX_SERVER not configured")
        return None
    url = f"{matrix_server}/_matrix/client/r0/{endpoint}"
    try:
        if method == "GET":
            r = requests.get(url, headers=matrix_headers, params=params, timeout=30, verify=matrix_verify_ssl)
        elif method == "POST":
            r = requests.post(url, headers=matrix_headers, json=data, timeout=30, verify=matrix_verify_ssl)
        elif method == "PUT":
            r = requests.put(url, headers=matrix_headers, json=data, timeout=30, verify=matrix_verify_ssl)
        else:
            print(f"ERROR: Unsupported HTTP method: {method}")
            return None

        print(f"Matrix API {method} {endpoint}: {r.status_code}")
        
        if r.status_code in [200, 201]:
            try:
                return r.json()
            except json.JSONDecodeError:
                print(f"Matrix API {method} {endpoint} returned invalid JSON: {r.text[:200]}")
                return None

        print(f"Matrix API {method} call {endpoint} failed: {r.status_code} - {r.text[:200]}")
        return None
    except requests.exceptions.SSLError as e:
        print(f"ERROR: SSL/TLS error connecting to {matrix_server}")
        print(f"SSL error: {e}")
        if matrix_verify_ssl:
            print(f"Tip: Set MATRIX_VERIFY_SSL=false to disable SSL verification (for self-signed certs)")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"ERROR: Failed to connect to Matrix server {matrix_server}")
        print(f"Connection error: {e}")
        return None
    except requests.exceptions.Timeout:
        print(f"ERROR: Request to {matrix_server} timed out")
        return None
    except Exception as e:
        print(f"Matrix request exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_own_account():
    result = matrix_request("GET", "account/whoami")
    if result:
        user_id = result.get("user_id")
        print(f"Bot user ID: {user_id}")
        account_info = {"user_id": user_id}
        # Fetch profile to get avatar URL
        if user_id:
            profile = matrix_request("GET", f"profile/{user_id}")
            if profile:
                avatar_mxc = profile.get("avatar_url")
                if avatar_mxc and avatar_mxc.startswith("mxc://"):
                    # Convert mxc:// URL to https:// URL
                    # mxc://server/media_id -> https://server/_matrix/media/v3/download/server/media_id
                    mxc_parts = avatar_mxc[6:].split("/", 1)  # Remove "mxc://"
                    if len(mxc_parts) == 2:
                        server, media_id = mxc_parts
                        account_info["avatar_url"] = f"https://{server}/_matrix/media/v3/download/{server}/{media_id}"
                        print(f"Bot avatar URL: {account_info['avatar_url']}")
        return account_info
    return None

def join_room(room_id, via=None):
    """Accept a room invite / join a room by ID or #alias.

    `via` is an optional list of homeserver names to route the join through over
    federation. For a bare room ID (!id:server) the server portion is added
    automatically as a hint so the homeserver knows where to find the room.

    Returns (result_dict_or_None, error_string_or_None). On success the error is
    None; on failure result is None and error holds the server's reason (so callers
    can tell the user *why* it failed — e.g. a restricted/space-gated room).
    """
    # safe='' so '!', '#', ':' in room IDs/aliases are percent-encoded for the path
    encoded = quote(room_id, safe='')

    # Build server_name federation hints: explicit via servers + the room ID's own domain
    servers = list(via) if via else []
    if room_id.startswith("!") and ":" in room_id:
        domain = room_id.split(":", 1)[1]
        if domain not in servers:
            servers.append(domain)
    params = {"server_name": servers} if servers else None

    if not matrix_server:
        return None, "MATRIX_SERVER not configured"
    url = f"{matrix_server}/_matrix/client/r0/join/{encoded}"
    try:
        r = requests.post(url, headers=matrix_headers, params=params, json={},
                          timeout=30, verify=matrix_verify_ssl)
        print(f"Matrix API POST join/{room_id}: {r.status_code}")
        if r.status_code in (200, 201):
            print(f"Joined room: {room_id}")
            try:
                return r.json(), None
            except json.JSONDecodeError:
                return {"room_id": room_id}, None
        # Surface the homeserver's error reason
        reason = r.text[:300]
        try:
            reason = r.json().get("error", reason)
        except Exception:
            pass
        print(f"Failed to join room {room_id}: {r.status_code} - {reason}")
        return None, reason
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"Failed to join room {room_id}: {err}")
        return None, err


def resolve_room_alias(alias):
    """Resolve a #alias:server to its room_id, or None if it can't be resolved."""
    encoded = quote(alias, safe='')
    result = matrix_request("GET", f"directory/room/{encoded}")
    if result:
        return result.get("room_id")
    return None


def leave_room(room_id_or_alias):
    """Leave a room by ID or #alias. Returns True on success, False otherwise."""
    target = room_id_or_alias
    if target.startswith("#"):
        resolved = resolve_room_alias(target)
        if not resolved:
            print(f"Failed to resolve alias for leave: {target}")
            return False
        target = resolved
    encoded = quote(target, safe='')
    # /leave returns an empty {} on success — matrix_request gives None only on failure
    result = matrix_request("POST", f"rooms/{encoded}/leave", data={})
    if result is not None:
        print(f"Left room: {target}")
        return True
    print(f"Failed to leave room: {target}")
    return False


def _invite_inviter(invite_data):
    """Extract the inviter's mxid from an invited room's stripped state, or None."""
    events = (invite_data or {}).get("invite_state", {}).get("events", [])
    # The bot's own m.room.member invite event — its sender is who invited us.
    for ev in events:
        if (ev.get("type") == "m.room.member"
                and ev.get("state_key") == MATRIX_USER_ID
                and (ev.get("content") or {}).get("membership") == "invite"):
            return ev.get("sender")
    # Fallback: any invite-membership event's sender.
    for ev in events:
        if ev.get("type") == "m.room.member" and (ev.get("content") or {}).get("membership") == "invite":
            return ev.get("sender")
    return None


def _accept_invites(invites):
    """Accept room invites ONLY from admins; decline (reject) all others so non-admins
    (including blocked users) can't pull the bot into rooms. An admin can still add the
    bot to any room by inviting it themselves or with the `join <roomid>` command."""
    if not invites:
        return
    admins = set(MATRIX_ADMINS or [])
    for inv_room_id, inv_data in invites.items():
        inviter = _invite_inviter(inv_data)
        if inviter and inviter in admins:
            print(f"✅ Accepting invite to {inv_room_id} from admin {inviter}")
            join_room(inv_room_id)
        else:
            print(f"🚫 Declining invite to {inv_room_id} from non-admin {inviter}")
            leave_room(inv_room_id)


def get_room_member_count(room_id):
    """Return the number of joined members in a room (used to identify DM rooms)."""
    encoded = quote(room_id)
    result = matrix_request("GET", f"rooms/{encoded}/joined_members")
    if result:
        return len(result.get("joined", {}))
    return 0


def get_sync_token():
    """Get the next_batch token for sync, auto-accepting any pending invites."""
    result = matrix_request("GET", "sync", params={"timeout": 0})
    if result:
        token = result.get("next_batch")
        print(f"Initial sync token: {token}")
        # Process any pending invites in the initial sync (declines blocked inviters)
        invites = result.get("rooms", {}).get("invite", {})
        if invites:
            print(f"[init] Processing {len(invites)} pending invite(s)...")
            _accept_invites(invites)
        return token
    return None

def get_messages(since_token=None, timeout=30000):
    """Sync messages from Matrix rooms"""
    params = {"timeout": timeout}
    if since_token:
        params["since"] = since_token
    
    print(f"Syncing messages (since={since_token})...")
    result = matrix_request("GET", "sync", params=params)
    
    if not result:
        return [], None
    
    messages = []
    rooms = result.get("rooms", {}).get("join", {})

    print(f"Processing {len(rooms)} joined rooms...")
    if rooms:
        print(f"Room IDs: {list(rooms.keys())}")

    # Auto-accept pending invites so the bot can receive DMs and new room messages
    # (invites from blocked users are declined instead of joined)
    invites = result.get("rooms", {}).get("invite", {})
    if invites:
        print(f"Processing {len(invites)} room invite(s)...")
        _accept_invites(invites)

    # Detect room member counts from summary (to identify DM rooms)
    for room_id, room_data in rooms.items():
        summary = room_data.get("summary", {})
        member_count = summary.get("m.joined_member_count", 0)
        room_data["_member_count"] = member_count

    for room_id, room_data in rooms.items():
        timeline = room_data.get("timeline", {})
        events = timeline.get("events", [])
        
        print(f"Room {room_id}: {len(events)} events")
        
        for event in events:
            event_type = event.get("type")
            print(f"  Event type: {event_type}")
            
            if event_type == "m.room.encrypted":
                # Bot cannot decrypt E2EE messages. Emit a synthetic plain-text message
                # so the listener can send an unencrypted reply asking the user to
                # create a non-encrypted DM room.
                sender = event.get("sender")
                print(f"  Encrypted message from {sender} — cannot decrypt")
                messages.append({
                    "event_id": event.get("event_id"),
                    "room_id": room_id,
                    "sender": sender,
                    "content": "__encrypted__",
                    "formatted_content": "",
                    "mentioned_users": [],
                    "timestamp": event.get("origin_server_ts", 0),
                    "room_member_count": room_data.get("_member_count", 0),
                    "is_encrypted": True,
                })

            elif event_type == "m.reaction":
                # Annotation (❤/👍/🔁 …) on another event. Carries the target event id and
                # the emoji key in m.relates_to; relayed to the timeline bridge as like/boost.
                _rel = (event.get("content", {}) or {}).get("m.relates_to", {}) or {}
                messages.append({
                    "event_id": event.get("event_id"),
                    "room_id": room_id,
                    "sender": event.get("sender"),
                    "content": "",
                    "event_type": "reaction",
                    "reaction_key": _rel.get("key", ""),
                    "reaction_target": _rel.get("event_id"),
                    "timestamp": event.get("origin_server_ts", 0),
                    "room_member_count": room_data.get("_member_count", 0),
                })

            elif event_type == "m.room.member":
                _content = event.get("content", {})
                _membership = _content.get("membership")
                _state_key = event.get("state_key")
                _prev = event.get("unsigned", {}).get("prev_content", {})
                _prev_membership = _prev.get("membership") if _prev else None
                print(f"  Member event: {_state_key} membership={_membership} prev_membership={_prev_membership}")
                if _membership == "join" and _state_key != MATRIX_USER_ID:
                    messages.append({
                        "event_id": event.get("event_id"),
                        "room_id": room_id,
                        "sender": _state_key,
                        "content": f"{_state_key} joined",
                        "event_type": "join",
                        "timestamp": event.get("origin_server_ts", 0),
                        "room_member_count": room_data.get("_member_count", 0),
                    })
                    print(f"  → Join event queued: {_state_key} joined {room_id}")

            elif event_type == "m.room.message":
                content = event.get("content", {})
                msgtype = content.get("msgtype")
                body = content.get("body", "")
                sender = event.get("sender")

                # Get formatted_body for mentions (Matrix stores mentions in HTML format)
                formatted_body = content.get("formatted_body", "")
                # Get m.mentions field (modern Matrix way to track mentions)
                mentions = content.get("m.mentions", {})
                mentioned_users = mentions.get("user_ids", []) if mentions else []

                # If this message is a reply, capture the event it replies to
                # (used by the `translate` command to act on the quoted message).
                _relates = content.get("m.relates_to", {}) or {}
                _in_reply = _relates.get("m.in_reply_to", {}) or {}
                reply_to_event_id = _in_reply.get("event_id")
                # Thread root: in a thread, m.in_reply_to points at the LAST event in the
                # thread (often an untracked media child), while m.relates_to.event_id is the
                # actual thread root — the reliable target for relaying a reply.
                thread_root_event_id = _relates.get("event_id") if _relates.get("rel_type") == "m.thread" else None

                print(f"  Message from {sender}: {body[:50]}...")
                print(f"  Full content object: {content}")
                print(f"  Mentioned users: {mentioned_users}")

                if msgtype == "m.text":
                    messages.append({
                        "event_id": event.get("event_id"),
                        "room_id": room_id,
                        "sender": sender,
                        "content": body,
                        "formatted_content": formatted_body,
                        "mentioned_users": mentioned_users,
                        "timestamp": event.get("origin_server_ts", 0),
                        "room_member_count": room_data.get("_member_count", 0),
                        "reply_to_event_id": reply_to_event_id,
                        "thread_root_event_id": thread_root_event_id,
                    })
                elif msgtype in ("m.image", "m.file", "m.video", "m.audio"):
                    # Media attachment (image/file/video/audio). The bot forwards
                    # these to posterchanai for the compress/convert commands.
                    # Per MSC2530, when the upload has a caption the event `body`
                    # holds the caption and `filename` holds the real filename;
                    # otherwise `body` is the filename and there is no caption.
                    info = content.get("info", {}) or {}
                    real_filename = content.get("filename")
                    caption = body if real_filename else ""
                    messages.append({
                        "event_id": event.get("event_id"),
                        "room_id": room_id,
                        "sender": sender,
                        "content": caption,
                        "formatted_content": formatted_body,
                        "mentioned_users": mentioned_users,
                        "timestamp": event.get("origin_server_ts", 0),
                        "room_member_count": room_data.get("_member_count", 0),
                        "reply_to_event_id": reply_to_event_id,
                        "thread_root_event_id": thread_root_event_id,
                        "attachment": {
                            "mxc_url": content.get("url"),
                            "filename": real_filename or body or "file",
                            "mimetype": info.get("mimetype", ""),
                            "msgtype": msgtype,
                        },
                    })
    
    next_batch = result.get("next_batch")
    print(f"Found {len(messages)} text messages, next_batch: {next_batch}")
    return messages, next_batch

def upload_media_to_matrix(image_bytes, filename="image.png", mime="image/png"):
    """Upload media to Matrix homeserver"""
    if not matrix_server:
        print("ERROR: MATRIX_SERVER not configured, cannot upload media")
        return None
    
    # URL-encode filename to prevent path traversal
    # Try authenticated client media endpoint first (v1), fallback to legacy media endpoint (r0)
    endpoints = [
        f"{matrix_server}/_matrix/client/v1/media/upload?filename={quote(filename)}",
        f"{matrix_server}/_matrix/media/v3/upload?filename={quote(filename)}",
        f"{matrix_server}/_matrix/media/r0/upload?filename={quote(filename)}",
    ]
    # Ensure we have raw bytes - extract from tuple if needed (MUST be before any use)
    if isinstance(image_bytes, tuple):
        print(f"WARNING: upload_media_to_matrix received tuple, extracting bytes")
        image_bytes = image_bytes[0] if image_bytes else None
    
    if not isinstance(image_bytes, bytes):
        print(f"ERROR: upload_media_to_matrix received {type(image_bytes).__name__}, expected bytes")
        return None
    
    headers = {
        "Authorization": f"Bearer {matrix_token}",
        "Content-Type": mime
    }
    
    print(f"Uploading media to {matrix_server}: {filename} ({len(image_bytes)} bytes)")
    
    for url in endpoints:
        try:
            print(f"Trying endpoint: {url.split('?')[0]}")
            print(f"  Data type: {type(image_bytes).__name__}, length: {len(image_bytes)}")
            r = requests.post(url, data=image_bytes, headers=headers, timeout=120, verify=matrix_verify_ssl)
            if r.status_code == 200:
                try:
                    res = r.json()
                    content_uri = res.get("content_uri")
                    print(f"Media uploaded successfully: {content_uri}")
                    return content_uri
                except json.JSONDecodeError:
                    print(f"Invalid JSON in media upload response: {r.text[:200]}")
                    continue
            elif r.status_code == 404:
                print(f"Endpoint not found (404), trying next...")
                continue
            else:
                print(f"Media upload failed: HTTP {r.status_code}")
                print(f"Response: {r.text[:500]}")
                # Try next endpoint for 4xx errors (except 401/403 which are auth issues)
                if r.status_code in [401, 403]:
                    return None
                continue
        except requests.exceptions.Timeout:
            print(f"ERROR: Media upload timed out after 120 seconds")
            continue
        except requests.exceptions.SSLError as e:
            print(f"ERROR: SSL/TLS error: {e}")
            if matrix_verify_ssl:
                print(f"Tip: Set MATRIX_VERIFY_SSL=false for self-signed certs")
            return None  # SSL errors won't be fixed by trying different endpoints
        except requests.exceptions.ConnectionError as e:
            print(f"ERROR: Connection error: {e}")
            continue
        except Exception as e:
            print(f"ERROR: Media upload exception: {type(e).__name__}: {e}")
            continue
    
    print("ERROR: All media upload endpoints failed")
    return None

def send_poll(room_id, question, options, max_selections=1, disclosed=True):
    """Post a native Matrix poll (m.poll.start, MSC3381 / Matrix 1.7).

    Element renders a live voting UI and tallies results itself, so the bot does
    no counting. `options` is a list of 2-20 answer strings. A plain-text fallback
    is included so non-poll-aware clients still see the question and choices.
    Returns True on success, False otherwise.
    """
    opts = [o.strip() for o in (options or []) if o and o.strip()][:20]
    if not question or not question.strip():
        print("Poll: empty question")
        return False
    if len(opts) < 2:
        print("Poll: need at least 2 options")
        return False

    # Use the UNSTABLE msc3381 namespace — Element renders this far more reliably
    # than the stable m.poll.start, which can show as an empty event on many clients.
    TEXT = "org.matrix.msc1767.text"
    answers = [{"id": f"opt{i}", TEXT: o} for i, o in enumerate(opts)]
    fallback = question.strip() + "\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(opts))
    content = {
        "org.matrix.msc3381.poll.start": {
            "question": {TEXT: question.strip()},
            "kind": "org.matrix.msc3381.poll.disclosed" if disclosed else "org.matrix.msc3381.poll.undisclosed",
            "max_selections": max(1, int(max_selections)),
            "answers": answers,
        },
        TEXT: fallback,
        # Plain body so clients with no poll support at all still show something
        "body": fallback,
    }
    txn_id = str(uuid.uuid4())
    result = matrix_request("PUT",
        f"rooms/{room_id}/send/org.matrix.msc3381.poll.start/{txn_id}", data=content)
    if result:
        print(f"✓ Poll sent to {room_id}")
        return True
    print(f"✗ Failed to send poll to {room_id}")
    return False


def send_message(room_id, text, reply_to=None, image_bytes=None, audio_bytes=None, video_bytes=None, image_caption=None, mentions=None):
    """Send a message to a Matrix room.

    image_caption: when an image is sent, render this text as a caption on the
    SAME image event (MSC2530 — `filename` + caption `body`/`formatted_body`) so
    the image and its text/link appear as ONE post, instead of a separate text
    message followed by a separate image. The caption's formatted_body is built
    from Markdown so bare links stay clickable beneath the image in Element.
    """
    if not text and not image_bytes and not audio_bytes and not video_bytes:
        print("No content to send")
        return False

    # Prevent sending any message that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and ((text and BLOCK_PHRASE in text) or (image_caption and BLOCK_PHRASE in image_caption)):
        print("Message contains blocked phrase; not sending to Matrix.")
        return False

    print(f"Sending message to room {room_id}")
    print(f"Text: {text[:100] if text else 'None'}...")
    print(f"Reply to: {reply_to}")
    print(f"Has image: {image_bytes is not None}")
    if image_bytes:
        if isinstance(image_bytes, list):
            print(f"  Image list length: {len(image_bytes)}")
            for i, img in enumerate(image_bytes):
                if isinstance(img, tuple) and len(img) == 2:
                    img_b, mime = img
                    print(f"  Image {i}: tuple, bytes_size={len(img_b) if isinstance(img_b, bytes) else 'N/A'}, mime={mime}")
                elif isinstance(img, bytes):
                    print(f"  Image {i}: bytes, size={len(img)}")
                else:
                    print(f"  Image {i}: type={type(img).__name__}")
        else:
            print(f"  Single image: type={type(image_bytes).__name__}, size={len(image_bytes) if isinstance(image_bytes, bytes) else 'N/A'}")
    print(f"Has audio: {audio_bytes is not None}")
    print(f"Has video: {video_bytes is not None}")

    # Send text message
    if text:
        content = {
            "msgtype": "m.text",
            "body": text,
            # Rich body so Element renders Markdown (links/bold) and keeps each
            # line separated instead of collapsing newlines into one blob.
            "format": "org.matrix.custom.html",
            "formatted_body": markdown_to_matrix_html(text),
        }

        if reply_to:
            content["m.relates_to"] = {
                "m.in_reply_to": {
                    "event_id": reply_to
                }
            }

        if mentions:
            content["m.mentions"] = {"user_ids": mentions}

        txn_id = str(uuid.uuid4())
        result = matrix_request("PUT", f"rooms/{room_id}/send/m.room.message/{txn_id}", data=content)

        if result:
            print("✓ Message sent successfully")
        else:
            print("✗ Failed to send message")
            return False

    # Send image if provided (supports single bytes, list of bytes, or list of (bytes, mime) tuples)
    if image_bytes:
        # Normalize to a list for uniform handling
        if isinstance(image_bytes, list):
            images_to_send = image_bytes
        else:
            images_to_send = [image_bytes]

        print(f"[send_message] Processing {len(images_to_send)} images...")

        for idx, img_item in enumerate(images_to_send):
            # Handle both plain bytes and (bytes, mime) tuples
            if isinstance(img_item, tuple) and len(img_item) == 2:
                img_bytes, mime_type = img_item
                if mime_type is None:
                    mime_type = "image/png"
            elif isinstance(img_item, bytes):
                img_bytes = img_item
                mime_type = "image/png"
                # Sniff actual format from magic bytes — a declared/real mismatch
                # makes Element show the image as a download attachment.
                if img_bytes[:3] == b"\xff\xd8\xff":
                    mime_type = "image/jpeg"
                elif img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    mime_type = "image/png"
                elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
                    mime_type = "image/gif"
                elif img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
                    mime_type = "image/webp"
            else:
                print(f"✗ Skipping invalid image data at index {idx} (type: {type(img_item).__name__})")
                continue

            if not isinstance(img_bytes, bytes):
                print(f"✗ Skipping invalid image bytes at index {idx} (type: {type(img_bytes).__name__})")
                continue

            # Determine file extension from mime type
            ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
            ext = ext_map.get(mime_type, "png")
            filename = f"image_{idx + 1}.{ext}"

            print(f"[send_message] Uploading image {idx + 1}: {len(img_bytes)} bytes, mime={mime_type}")
            media_uri = upload_media_to_matrix(img_bytes, filename=filename, mime=mime_type)
            if media_uri:
                _info = {"mimetype": mime_type, "size": len(img_bytes)}
                # Add pixel dimensions — some Matrix clients require w/h to render inline
                try:
                    from PIL import Image as _PILImage
                    from io import BytesIO as _BytesIO
                    with _PILImage.open(_BytesIO(img_bytes)) as _im:
                        _info["w"], _info["h"] = _im.width, _im.height
                except Exception as _dim_err:
                    print(f"[send_message] Could not read image dimensions: {_dim_err}")
                content = {
                    "msgtype": "m.image",
                    "body": filename,
                    "url": media_uri,
                    "info": _info,
                }
                # Caption: attach the text/link to the image event itself so they
                # render as a single post. Per MSC2530, when `filename` is set the
                # `body` is treated as a caption; the HTML formatted_body keeps the
                # link clickable. Applied to the first image only.
                if image_caption and idx == 0:
                    content["filename"] = filename
                    content["body"] = image_caption
                    content["format"] = "org.matrix.custom.html"
                    content["formatted_body"] = markdown_to_matrix_html(image_caption)

                txn_id = str(uuid.uuid4())
                result = matrix_request("PUT", f"rooms/{room_id}/send/m.room.message/{txn_id}", data=content)

                if result:
                    print(f"✓ Image {idx + 1}/{len(images_to_send)} sent successfully")
                else:
                    print(f"✗ Failed to send image {idx + 1}/{len(images_to_send)}")
            else:
                print(f"✗ Failed to upload image {idx + 1}/{len(images_to_send)}")

    # Send video if provided (takes priority over audio)
    if video_bytes:
        media_uri = upload_media_to_matrix(video_bytes, filename="narration.mp4", mime="video/mp4")
        if media_uri:
            content = {
                "msgtype": "m.video",
                "body": "narration.mp4",
                "url": media_uri,
                "info": {
                    "mimetype": "video/mp4",
                    "size": len(video_bytes)
                }
            }

            txn_id = str(uuid.uuid4())
            result = matrix_request("PUT", f"rooms/{room_id}/send/m.room.message/{txn_id}", data=content)

            if result:
                print("✓ Video sent successfully")
            else:
                print("✗ Failed to send video")
                return False
        else:
            print("✗ Video upload failed (no media URI)")
            return False
    # Send audio if provided (fallback if no video)
    elif audio_bytes:
        media_uri = upload_media_to_matrix(audio_bytes, filename="voice.mp3", mime="audio/mpeg")
        if media_uri:
            content = {
                "msgtype": "m.audio",
                "body": "voice.mp3",
                "url": media_uri,
                "info": {
                    "mimetype": "audio/mpeg",
                    "size": len(audio_bytes)
                }
            }

            txn_id = str(uuid.uuid4())
            result = matrix_request("PUT", f"rooms/{room_id}/send/m.room.message/{txn_id}", data=content)

            if result:
                print("✓ Audio sent successfully")
            else:
                print("✗ Failed to send audio")
                return False
        else:
            print("✗ Audio upload failed (no media URI)")
            return False

    return True


def _msgtype_for_mime(mime):
    """Map a MIME type to the appropriate Matrix message type."""
    mime = mime or ""
    if mime.startswith("image/"):
        return "m.image"
    if mime.startswith("video/"):
        return "m.video"
    if mime.startswith("audio/"):
        return "m.audio"
    return "m.file"


def send_file_to_room(room_id, file_bytes, filename, mime="application/octet-stream"):
    """Upload arbitrary file bytes and post them into a room AS THE BOT.

    Picks the message type (m.image/m.video/m.audio/m.file) from the MIME type.
    Returns True on success. Used to deliver compress/convert results.
    """
    if not room_id or not file_bytes:
        return False
    media_uri = upload_media_to_matrix(file_bytes, filename=filename, mime=mime)
    if not media_uri:
        print(f"✗ File upload failed for {filename}")
        return False
    content = {
        "msgtype": _msgtype_for_mime(mime),
        "body": filename,
        "url": media_uri,
        "info": {"mimetype": mime, "size": len(file_bytes)},
    }
    txn_id = str(uuid.uuid4())
    result = matrix_request("PUT", f"rooms/{room_id}/send/m.room.message/{txn_id}", data=content)
    if result:
        print(f"✓ Sent file {filename} to room")
        return True
    print(f"✗ Failed to send file {filename} to room")
    return False


def send_reply(message_obj, reply_text, image_bytes=None, audio_bytes=None, video_bytes=None):
    """Reply to a specific message"""
    room_id = message_obj.get("room_id")
    event_id = message_obj.get("event_id")
    print(f"[send_reply] event={event_id}, room={room_id}")
    print(f"[send_reply] image_bytes: {type(image_bytes).__name__}, is None: {image_bytes is None}")
    if image_bytes is not None:
        if isinstance(image_bytes, list):
            print(f"[send_reply] image_bytes is list with {len(image_bytes)} items")
            for i, item in enumerate(image_bytes):
                if isinstance(item, tuple):
                    print(f"[send_reply]   item {i}: tuple")
                elif isinstance(item, bytes):
                    print(f"[send_reply]   item {i}: bytes, len={len(item)}")
                else:
                    print(f"[send_reply]   item {i}: {type(item).__name__}")
        else:
            print(f"[send_reply] image_bytes is single item")
    return send_message(room_id, reply_text, reply_to=event_id, image_bytes=image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)

def post_to_matrix(room_id, text):
    """Post a message to a specific Matrix room"""
    return send_message(room_id, text)

def post_image_to_matrix(room_id, text, image_bytes=None):
    """Post text and/or image to a Matrix room"""
    return send_message(room_id, text, image_bytes=image_bytes)

def get_event(room_id, event_id):
    """Fetch a single event by ID from a room"""
    # URL encode the event ID (contains $ and other special chars)
    encoded_event_id = quote(event_id, safe='')
    encoded_room_id = quote(room_id, safe='')

    # Try direct event fetch first
    result = matrix_request("GET", f"rooms/{encoded_room_id}/event/{encoded_event_id}")
    if result:
        return result

    # Fallback: try context API which may work better for federated events
    print(f"[get_event] Direct fetch failed, trying context API for {event_id}")
    context = matrix_request("GET", f"rooms/{encoded_room_id}/context/{encoded_event_id}", params={"limit": 0})
    if context and context.get("event"):
        print(f"[get_event] Found event via context API")
        return context.get("event")

    print(f"[get_event] Could not fetch event {event_id}")
    return None

def download_image_from_url(url, timeout=30):
    """
    Download an image from a URL and return the raw bytes.
    Returns None if download fails.
    """
    try:
        # Include Matrix auth header if downloading from Matrix media endpoint
        headers = {}
        if matrix_token and "/_matrix/" in url:
            headers["Authorization"] = f"Bearer {matrix_token}"

        r = requests.get(url, headers=headers, timeout=timeout, verify=matrix_verify_ssl)
        if r.status_code == 200:
            return r.content
        print(f"Failed to download image from {url}: {r.status_code}")
    except requests.exceptions.SSLError as e:
        print(f"SSL error downloading image from {url}: {e}")
        if matrix_verify_ssl:
            print(f"Tip: Set MATRIX_VERIFY_SSL=false to disable SSL verification")
    except requests.exceptions.RequestException as e:
        print(f"Failed to download image from {url}: {e}")
    return None


def mxc_to_https(mxc_url):
    """
    Convert an mxc:// URL to an HTTPS download URL.
    Uses the authenticated client media endpoint.
    mxc://server/media_id -> https://server/_matrix/client/v1/media/download/server/media_id
    """
    if not mxc_url or not mxc_url.startswith("mxc://"):
        return None
    # Remove "mxc://"
    mxc_path = mxc_url[6:]
    parts = mxc_path.split("/", 1)
    if len(parts) != 2:
        return None
    server, media_id = parts
    # Use authenticated client media endpoint
    return f"https://{server}/_matrix/client/v1/media/download/{server}/{media_id}"


def get_event_images(event_obj):
    """
    Extract image URL from a Matrix event.
    Returns list of dicts: [{"url": str, "type": str}, ...] or empty list
    """
    images = []
    content = event_obj.get("content", {})
    msgtype = content.get("msgtype", "")

    # Standard image message
    if msgtype == "m.image":
        mxc_url = content.get("url")
        if mxc_url:
            https_url = mxc_to_https(mxc_url)
            if https_url:
                images.append({"url": https_url, "type": "image"})

    # File attachment that is an image
    elif msgtype == "m.file":
        info = content.get("info", {})
        mimetype = info.get("mimetype", "")
        if mimetype.startswith("image/"):
            mxc_url = content.get("url")
            if mxc_url:
                https_url = mxc_to_https(mxc_url)
                if https_url:
                    images.append({"url": https_url, "type": "image"})

    return images


def get_thread_images(room_id, event_id, max_depth=10):
    """
    Search the thread for images, starting from the replied-to event.
    Returns the first image found as bytes, or None.
    """
    print(f"[get_thread_images] Starting search for room={room_id}, event={event_id}")
    event = get_event(room_id, event_id)
    if not event:
        print(f"[get_thread_images] Failed to fetch event {event_id}")
        return None

    print(f"[get_thread_images] Event content: {event.get('content', {})}")

    # Check if this event itself is an image
    images = get_event_images(event)
    if images:
        print(f"[get_thread_images] Current event has image: {images}")
        return download_image_from_url(images[0]["url"])

    # Check the replied-to event
    content = event.get("content", {})
    relates_to = content.get("m.relates_to", {})
    print(f"[get_thread_images] m.relates_to: {relates_to}")

    # Try m.in_reply_to first (standard replies)
    in_reply_to = relates_to.get("m.in_reply_to", {})
    reply_event_id = in_reply_to.get("event_id")
    print(f"[get_thread_images] m.in_reply_to event_id: {reply_event_id}")

    # If no reply, check for thread relationship (MSC3440)
    if not reply_event_id and relates_to.get("rel_type") == "m.thread":
        reply_event_id = relates_to.get("event_id")
        print(f"[get_thread_images] Using thread event_id: {reply_event_id}")

    if reply_event_id:
        print(f"[get_thread_images] Fetching reply event {reply_event_id}")
        reply_event = get_event(room_id, reply_event_id)
        if reply_event:
            print(f"[get_thread_images] Reply event content: {reply_event.get('content', {})}")
            print(f"[get_thread_images] Reply event msgtype: {reply_event.get('content', {}).get('msgtype')}")
            images = get_event_images(reply_event)
            print(f"[get_thread_images] Reply event images: {images}")
            if images:
                return download_image_from_url(images[0]["url"])
        else:
            print(f"[get_thread_images] Failed to fetch reply event {reply_event_id}")
    else:
        print(f"[get_thread_images] No reply event ID found")

    # Walk up the reply chain to find an image
    current_event_id = reply_event_id
    depth = 0
    while current_event_id and depth < max_depth:
        parent_event = get_event(room_id, current_event_id)
        if not parent_event:
            break

        images = get_event_images(parent_event)
        if images:
            return download_image_from_url(images[0]["url"])

        # Get next parent
        parent_content = parent_event.get("content", {})
        parent_relates = parent_content.get("m.relates_to", {})
        parent_reply = parent_relates.get("m.in_reply_to", {})
        current_event_id = parent_reply.get("event_id")
        depth += 1

    return None


def get_thread_history(room_id, event_id, max_depth=20):
    """
    Fetch the full conversation thread by walking back through reply chain.
    Returns a list of dicts: [{"username": str, "content": str, "is_bot": bool}, ...]
    Ordered from oldest to newest (root first).
    """
    import re
    import html as html_module

    def strip_html(html_text):
        if not html_text:
            return ""
        text = re.sub(r"<[^>]+>", "", html_text)
        return html_module.unescape(text).strip()

    thread = []
    current_event_id = event_id
    depth = 0

    # Get own user ID to identify bot messages
    own_account = get_own_account()
    own_user_id = own_account.get("user_id") if own_account else None

    while current_event_id and depth < max_depth:
        event = get_event(room_id, current_event_id)
        if not event:
            break

        # Only process message events
        if event.get("type") != "m.room.message":
            break

        sender = event.get("sender", "unknown")
        content_obj = event.get("content", {})
        body = content_obj.get("body", "")

        # Strip HTML if formatted body exists
        formatted_body = content_obj.get("formatted_body")
        if formatted_body:
            body = strip_html(formatted_body)

        # Extract username from full Matrix ID (@user:server.com -> user)
        username = sender.split(':')[0] if sender else "unknown"
        is_bot = (sender == own_user_id)

        thread.append({
            "username": username,
            "content": body,
            "is_bot": is_bot
        })

        # Get the reply parent event ID
        relates_to = content_obj.get("m.relates_to", {})
        in_reply_to = relates_to.get("m.in_reply_to", {})
        current_event_id = in_reply_to.get("event_id")
        depth += 1

    # Reverse to get oldest first
    thread.reverse()
    return thread