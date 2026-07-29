"""
Posterchanai API client for image generation.
Image generation via posterchanai's native diffusers backend (the one image backend).
Includes native WD14 tagging support.
"""
import json
import base64
import time
from urllib import request
from config import POSTERCHANAI_API_ENDPOINT, POSTERCHANAI_API_KEY


def _get_auth_headers():
    """Get authorization headers for posterchanai API"""
    # Check if API key is set and not empty
    if POSTERCHANAI_API_KEY and POSTERCHANAI_API_KEY.strip():
        # Try X-API-Key header first (preferred for image API)
        return {
            "X-API-Key": POSTERCHANAI_API_KEY.strip(),
            "Content-Type": "application/json"
        }
    return {"Content-Type": "application/json"}


def _login_and_get_token():
    """Login to posterchanai and get access token"""
    from config import POSTERCHANAI_USERNAME, POSTERCHANAI_PASSWORD

    if not POSTERCHANAI_USERNAME or not POSTERCHANAI_PASSWORD:
        print("[POSTERCHANAI] POSTERCHANAI_USERNAME and POSTERCHANAI_PASSWORD must be set in config.py")
        return None

    url = f"{POSTERCHANAI_API_ENDPOINT}/api/auth/login"
    data = json.dumps({
        "username": POSTERCHANAI_USERNAME,
        "password": POSTERCHANAI_PASSWORD
    }).encode('utf-8')

    try:
        req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
        response = request.urlopen(req, timeout=30)
        result = json.loads(response.read())
        token = result.get("access_token")
        if not token:
            print("[POSTERCHANAI] Login succeeded but no access_token in response")
        return token
    except request.HTTPError as e:
        if e.code == 401:
            print(f"[POSTERCHANAI] Login failed: Invalid username or password (401)")
            print(f"[POSTERCHANAI] Check POSTERCHANAI_USERNAME and POSTERCHANAI_PASSWORD in config.py")
        else:
            print(f"[POSTERCHANAI] Login failed: HTTP {e.code} - {e.reason}")
        return None
    except Exception as e:
        print(f"[POSTERCHANAI] Login error: {e}")
        return None


def _compress_generated(data: bytes) -> bytes:
    """Run a freshly generated image through the app's own compressor before a bot uploads it.

    The backend returns a full-size PNG, which is what every bot was posting: each one costs the
    instance its full size on upload and again for every fetch by every federating server. This is
    the SAME `media_service.compress_image` the `compress` command uses (longest edge 2048, JPEG
    q70), so bot images get the compression the rest of the app already applies.

    Best-effort by design: if Pillow or the app package isn't importable in this process, or the
    bytes aren't a decodable image, return the ORIGINAL. A bot must still be able to post — a
    bandwidth optimisation is never worth turning a working post into a failed one. Likewise, if the
    re-encode comes out BIGGER (already-optimised or small source), keep the original.
    """
    if not data:
        return data
    try:
        from app.services.media_service import compress_image
        out = compress_image(data)
        if out and len(out) < len(data):
            print(f"[POSTERCHANAI] compressed image {len(data)} -> {len(out)} bytes "
                  f"({100 - int(100 * len(out) / len(data))}% smaller)")
            return out
        return data
    except Exception as e:
        print(f"[POSTERCHANAI] image compression skipped ({type(e).__name__}: {e}) — posting original")
        return data


def generate_image_bytes(prompt):
    """
    Generate image using posterchanai's native diffusers backend.
    Returns image bytes or None. The bytes are compressed on the way out (see _compress_generated),
    so every bot image path — listeners, image poster, DVM — gets it from this one place.
    """
    try:
        # Check if API key is set and not empty
        has_api_key = POSTERCHANAI_API_KEY and POSTERCHANAI_API_KEY.strip()
        
        # Clean prompt (remove "geni" if present)
        clean_prompt = prompt.replace("geni", "").strip()

        # Use the direct image generation endpoint
        img_url = f"{POSTERCHANAI_API_ENDPOINT}/api/generate-image"
        # Optional negative prompt (image bots set IMAGE_POSTER_NEGATIVE via the manager). Only
        # image bots set this env, so other image flows are unaffected.
        import os as _os
        _payload = {"prompt": clean_prompt}
        _negative = (_os.getenv("IMAGE_POSTER_NEGATIVE", "") or "").strip()
        if _negative:
            _payload["negative_prompt"] = _negative
        img_data = json.dumps(_payload).encode('utf-8')
        
        print(f"[POSTERCHANAI] Connecting to: {POSTERCHANAI_API_ENDPOINT}")
        print(f"[POSTERCHANAI] Generating image with prompt: {clean_prompt[:100]}...")
        
        # Try with API key first (if available)
        headers = _get_auth_headers()
        img_req = request.Request(img_url, data=img_data, headers=headers, method='POST')
        
        try:
            img_resp = request.urlopen(img_req, timeout=300)
            result = json.loads(img_resp.read())
        except request.HTTPError as e:
            if e.code == 401:
                # Authentication required - try login if we haven't already
                if not has_api_key:
                    print(f"[POSTERCHANAI] API key not set, trying login authentication...")
                    try:
                        token = _login_and_get_token()
                        if token:
                            # Retry with JWT token
                            headers = {
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"
                            }
                            img_req = request.Request(img_url, data=img_data, headers=headers, method='POST')
                            img_resp = request.urlopen(img_req, timeout=300)
                            result = json.loads(img_resp.read())
                        else:
                            print(f"[POSTERCHANAI] Authentication failed. Options:")
                            print(f"[POSTERCHANAI] 1. Set POSTERCHANAI_API_KEY in config.py (recommended)")
                            print(f"[POSTERCHANAI] 2. Set IMAGE_API_KEY in posterchanai and match it in config.py")
                            print(f"[POSTERCHANAI] 3. Fix POSTERCHANAI_USERNAME and POSTERCHANAI_PASSWORD in config.py")
                            return None
                    except Exception as login_err:
                        print(f"[POSTERCHANAI] Login failed: {login_err}")
                        print(f"[POSTERCHANAI] Authentication failed. Set POSTERCHANAI_API_KEY in config.py or fix login credentials.")
                        return None
                else:
                    print(f"[POSTERCHANAI] API key authentication failed (401).")
                    print(f"[POSTERCHANAI] Check that POSTERCHANAI_API_KEY matches IMAGE_API_KEY in posterchanai.")
                    return None
            else:
                print(f"[POSTERCHANAI] HTTP error {e.code}: {e.reason}")
                # Try to read error message from response
                try:
                    error_body = e.read().decode('utf-8')
                    error_data = json.loads(error_body)
                    print(f"[POSTERCHANAI] Error: {error_data.get('detail', 'Unknown error')}")
                except:
                    pass
                return None

        # Check for error in response
        if result.get("error"):
            print(f"[POSTERCHANAI] Image generation error: {result['error']}")
            return None

        # Check for image in response (key is 'image', not 'image_url')
        image_b64 = result.get("image") or result.get("image_url")
        if image_b64:
            if image_b64.startswith("data:image"):
                # Extract base64 part
                image_b64 = image_b64.split(",", 1)[1]
            return _compress_generated(base64.b64decode(image_b64))

        print(f"[POSTERCHANAI] No image in response. Keys: {list(result.keys())}")
        return None

    except OSError as e:
        # Connection errors (errno 111 = Connection refused, errno 113 = No route to host, etc.)
        error_code = getattr(e, 'errno', None)
        if error_code == 111:
            print(f"[POSTERCHANAI] ERROR: Connection refused to {POSTERCHANAI_API_ENDPOINT}")
            print(f"[POSTERCHANAI] The posterchanai service is not running or not accessible at this address")
            print(f"[POSTERCHANAI] Troubleshooting:")
            print(f"[POSTERCHANAI]  1. Check if posterchanai service is running")
            print(f"[POSTERCHANAI]  2. Verify POSTERCHANAI_API_ENDPOINT is correct (currently: {POSTERCHANAI_API_ENDPOINT})")
            print(f"[POSTERCHANAI]  3. Test connectivity: curl {POSTERCHANAI_API_ENDPOINT}/api/health")
            print(f"[POSTERCHANAI]  4. Check firewall rules and network routing")
        elif error_code == 113:
            print(f"[POSTERCHANAI] ERROR: No route to host {POSTERCHANAI_API_ENDPOINT}")
            print(f"[POSTERCHANAI] Network routing issue - cannot reach the server")
        elif error_code == 110:
            print(f"[POSTERCHANAI] ERROR: Connection timed out to {POSTERCHANAI_API_ENDPOINT}")
            print(f"[POSTERCHANAI] The server is not responding within the timeout period")
        else:
            print(f"[POSTERCHANAI] ERROR: Connection error (errno {error_code}) to {POSTERCHANAI_API_ENDPOINT}: {e}")
        return None
    except Exception as e:
        print(f"[POSTERCHANAI] ERROR: Unexpected error generating image: {type(e).__name__}: {e}")
        print(f"[POSTERCHANAI] Endpoint: {POSTERCHANAI_API_ENDPOINT}")
        import traceback
        traceback.print_exc()
        return None


def generate_image_bytes_with_retries(prompt, max_retries=5, retry_delay=30):
    """Generate image with retries."""
    attempt = 0
    max_delay = 300

    while attempt < max_retries:
        attempt += 1
        print(f"[POSTERCHANAI] Image generation attempt {attempt}...")

        try:
            image_bytes = generate_image_bytes(prompt)
            if image_bytes:
                print(f"[POSTERCHANAI] Success on attempt {attempt}")
                return image_bytes
        except Exception as e:
            print(f"[POSTERCHANAI] Exception on attempt {attempt}: {e}")

        delay = min(retry_delay * (2 ** (attempt - 1)), max_delay)
        print(f"[POSTERCHANAI] Retrying in {delay} seconds...")
        time.sleep(delay)

    print(f"[POSTERCHANAI] Failed after {max_retries} attempts")
    return None


def describe_image_with_wd14(image_bytes, threshold=0.35):
    """
    Use posterchanai's native WD14 tagger to get tags from an image.
    Returns a comma-separated string of tags, or None on failure.
    """
    try:
        # Use API key auth (or open access if no key configured)
        headers = _get_auth_headers()

        # Encode image as base64
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')

        # Use tag-image endpoint
        url = f"{POSTERCHANAI_API_ENDPOINT}/api/tag-image"
        data = json.dumps({
            "image": image_b64,
            "threshold": threshold
        }).encode('utf-8')

        req = request.Request(url, data=data, headers=headers, method='POST')
        resp = request.urlopen(req, timeout=120)
        result = json.loads(resp.read())

        if result.get("tags"):
            tags = result["tags"]
            # Strip character names (they override other attributes)
            tags = strip_character_name(tags)
            print(f"[POSTERCHANAI] WD14 tags: {tags[:100]}...")
            return tags

        if result.get("error"):
            print(f"[POSTERCHANAI] WD14 error: {result['error']}")

        return None

    except Exception as e:
        print(f"[POSTERCHANAI] WD14 error: {e}")
        import traceback
        traceback.print_exc()
        return None


def strip_character_name(tags):
    """Remove character names from WD14 tags (they override other attributes)."""
    if not tags:
        return tags
    parts = tags.split(", ")
    # Character names often have underscores or parentheses
    filtered = [p for p in parts if not ("(" in p and ")" in p) and "_" not in p[:10]]
    if filtered:
        return ", ".join(filtered)
    return tags


def extract_prompt_from_image(image_bytes):
    """Extract prompt from PNG metadata if present."""
    try:
        import struct

        if image_bytes[:8] != b'\x89PNG\r\n\x1a\n':
            return None

        pos = 8
        while pos < len(image_bytes):
            if pos + 8 > len(image_bytes):
                break

            chunk_length = struct.unpack('>I', image_bytes[pos:pos+4])[0]
            chunk_type = image_bytes[pos+4:pos+8].decode('ascii', errors='ignore')

            if chunk_type == 'IEND':
                break

            chunk_data = image_bytes[pos+8:pos+8+chunk_length]

            if chunk_type == 'tEXt':
                null_pos = chunk_data.find(b'\x00')
                if null_pos != -1:
                    keyword = chunk_data[:null_pos].decode('latin-1', errors='ignore')
                    text = chunk_data[null_pos+1:].decode('latin-1', errors='ignore')
                    if keyword.lower() in ('prompt', 'parameters', 'positive'):
                        return text

            pos += 4 + 4 + chunk_length + 4

        return None
    except Exception:
        return None


def process_media(command, arg, media, brand_handle=None, brand_avatar=None):
    """Run a compress/clip/convert operation on the posterchanai backend.

    `media` is a list of (filename, data_bytes, content_type) tuples. Returns
    (summary_text, output_files) where output_files is a list of
    {"filename", "data" (raw bytes), "content_type"}. On failure returns
    (error_message, []). Shared by the Pleroma listener (and mirrors
    the shared backend command endpoint) so they all reuse the
    backend's single HW-accelerated ffmpeg/Pillow path.

    `brand_handle` (the fediverse poster's @handle) and `brand_avatar`
    ((bytes, content_type) of their profile pic) personalize the effect's outro
    end-card; omit them for the static "made with PosterChanAI" card.
    """
    url = f"{POSTERCHANAI_API_ENDPOINT}/api/media/process"
    body = {
        "command": command,
        "arg": arg or "",
        "media": [
            {
                "filename": fn,
                "data": base64.b64encode(data).decode("ascii"),
                "content_type": ct or "",
            }
            for (fn, data, ct) in (media or [])
        ],
    }
    if brand_handle:
        body["brand_handle"] = brand_handle
    if brand_avatar and brand_avatar[0]:
        body["brand_avatar"] = {
            "filename": "avatar",
            "data": base64.b64encode(brand_avatar[0]).decode("ascii"),
            "content_type": brand_avatar[1] or "",
        }
    data = json.dumps(body).encode("utf-8")
    try:
        req = request.Request(url, data=data, headers=_get_auth_headers(), method="POST")
        resp = request.urlopen(req, timeout=3600)  # ffmpeg transcodes can be slow
        result = json.loads(resp.read())
    except request.HTTPError as e:
        return (f"❌ Media request failed: HTTP {e.code} {e.reason}", [])
    except Exception as e:
        return (f"❌ Media request failed: {e}", [])

    if result.get("error"):
        return (f"❌ {result['error']}", [])
    out_files = []
    for f in result.get("files", []):
        try:
            out_files.append({
                "filename": f.get("filename", "file"),
                "data": base64.b64decode(f["data"]),
                "content_type": f.get("content_type", "application/octet-stream"),
            })
        except Exception:
            continue
    return (result.get("summary", ""), out_files)


def parse_ytdl_postaction(text):
    """Pull the URL and optional post-processing modifiers out of a ytdl arg.

    Recognizes (case-insensitive, in any order after the URL):
      clip <start> <end>  — trim the downloaded video
      compress            — shrink it (applied after clip)
    Returns (url, clip_str_or_None, compress_bool). `clip_str` is "start end"
    (e.g. "0:10 0:30") suitable for passing straight to the backend. Shared by
    the Pleroma listener so the syntax is identical.
    """
    import re as _re
    text = text or ""
    m = _re.search(r'https?://\S+', text)
    url = m.group(0) if m else text.strip()
    toks = text.split()
    low = [t.lower() for t in toks]
    compress = "compress" in low
    clip = None
    if "clip" in low:
        # Capture the 1–2 tokens after "clip". If only one (or none) was given the
        # backend's own validation returns a clear "clip needs <start> <end>" error,
        # rather than the modifier being silently dropped.
        i = low.index("clip")
        rest = [t for t in toks[i + 1:i + 3] if t.lower() not in ("compress",)]
        if rest:
            clip = " ".join(rest)
    return url, clip, compress


def fetch_ytdl_media(url, video=False, clip=None, compress=False):
    """Download YouTube/X media via posterchanai's /api/media/ytdl endpoint.

    Identity-agnostic (authenticated by the bot's API key, not a linked user), so
    the Pleroma listener uses it — mirrors the
    ytdl. Audio (MP3) by default; video=True fetches MP4. The optional clip
    ("start end") and compress modifiers post-process the video server-side
    (clip → compress). Returns (bytes, mime, None) on success or
    (None, None, error_str).
    """
    api = f"{POSTERCHANAI_API_ENDPOINT}/api/media/ytdl"
    data = json.dumps({
        "url": url or "", "video": bool(video),
        "clip": clip, "compress": bool(compress),
    }).encode("utf-8")
    try:
        req = request.Request(api, data=data, headers=_get_auth_headers(), method="POST")
        resp = request.urlopen(req, timeout=900)  # download + transcode can be slow
        result = json.loads(resp.read())
    except request.HTTPError as e:
        return (None, None, f"HTTP {e.code} {e.reason}")
    except Exception as e:
        return (None, None, str(e))

    if not result.get("ok"):
        return (None, None, result.get("error", "download failed"))
    try:
        return (base64.b64decode(result["data"]), result.get("mime"), None)
    except Exception as e:
        return (None, None, f"could not decode media: {e}")


def capture_screenshot(url):
    """Capture a full-page screenshot of a website on the posterchanai backend.

    Returns (png_bytes, None) on success or (None, error_message) on failure.
    Shared by the Pleroma listener, which reuses the backend's single headless
    Chrome/Firefox path.
    """
    api = f"{POSTERCHANAI_API_ENDPOINT}/api/media/screenshot"
    data = json.dumps({"url": url or ""}).encode("utf-8")
    try:
        req = request.Request(api, data=data, headers=_get_auth_headers(), method="POST")
        resp = request.urlopen(req, timeout=300)  # page render + settle can be slow
        result = json.loads(resp.read())
    except request.HTTPError as e:
        return (None, f"❌ Screenshot request failed: HTTP {e.code} {e.reason}")
    except Exception as e:
        return (None, f"❌ Screenshot request failed: {e}")

    if result.get("error") or not result.get("data"):
        return (None, f"❌ {result.get('error', 'screenshot failed')}")
    try:
        return (base64.b64decode(result["data"]), None)
    except Exception as e:
        return (None, f"❌ Could not decode screenshot: {e}")


def render_post_card(handle, text, display_name="", timestamp="", media_bytes=None,
                     media_ct=None, avatar_bytes=None, avatar_ct=None):
    """Render a tweet-style post card on the backend (a screenshot of HTML built from
    these fields) and return (png_bytes, None) or (None, error_message).

    Used by the Nitter poster so Pleroma gets an image of the post
    instead of a bare link — link previews fail because Nitter's status pages are
    empty. Tweet media and the author's profile picture are pre-fetched here and sent
    as bytes (the server does no outbound fetch). Mirrors capture_screenshot()'s shape.
    """
    api = f"{POSTERCHANAI_API_ENDPOINT}/api/media/render-post-card"
    body = {
        "handle": handle or "",
        "text": text or "",
        "display_name": display_name or "",
        "timestamp": timestamp or "",
    }
    if media_bytes:
        body["media"] = {
            "filename": "media",
            "data": base64.b64encode(media_bytes).decode("ascii"),
            "content_type": media_ct or "image/jpeg",
        }
    if avatar_bytes:
        body["avatar"] = {
            "filename": "avatar",
            "data": base64.b64encode(avatar_bytes).decode("ascii"),
            "content_type": avatar_ct or "image/jpeg",
        }
    data = json.dumps(body).encode("utf-8")
    try:
        req = request.Request(api, data=data, headers=_get_auth_headers(), method="POST")
        resp = request.urlopen(req, timeout=300)
        result = json.loads(resp.read())
    except request.HTTPError as e:
        return (None, f"render card failed: HTTP {e.code} {e.reason}")
    except Exception as e:
        return (None, f"render card failed: {e}")

    if result.get("error") or not result.get("data"):
        return (None, result.get("error", "render card failed"))
    try:
        return (base64.b64decode(result["data"]), None)
    except Exception as e:
        return (None, f"could not decode card: {e}")
