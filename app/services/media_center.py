"""Server media catalog and bounded, on-demand HLS transcoding.

Catalog/ACL documents are NIP-44 encrypted NIP-78 events, using the operator
storage key. Sharing is mediated by this server, not public relay publication.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from functools import lru_cache
from io import BytesIO
from contextlib import asynccontextmanager
from pathlib import Path

from app.services import nostr_store, settings_store
from app.services.nostr.nostr_service import to_pubkey_hex

NS = "pcai:media-center:"
PROFILES = {"360p": (640, 360, 450, 64), "480p": (854, 480, 900, 96),
            "720p": (1280, 720, 2000, 128), "1080p": (1920, 1080, 4500, 128)}
EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".mpg", ".mpeg",
              ".mp3", ".flac", ".m4a", ".ogg", ".wav", ".opus"}
# A LOGGER THAT EXISTS AT IMPORT TIME. The transcoder's only failure paths are inside `except`
# blocks, and this repo has already taken an outage from an `except` that called an undefined
# `logger` — the guard raised NameError and every request 502'd.
logger = logging.getLogger(__name__)

SEGMENT = 6
mutation_lock = asyncio.Lock()
DEFAULT_LIMITS = {"server_kbps": 20000, "viewer_kbps": 1600, "max_streams": 8,
                  "max_transcodes": 2, "cache_mb": 2048}
_sessions = {}
_active_transcodes = 0
_job_condition = asyncio.Condition()
_rate_lock = asyncio.Lock()
_rate_due = {}
_failed_encoders = {}
_catalog_cache = {}
_account_cache = {}          # account key -> (record, when it was read)
_segment_jobs = {}
art_slots = asyncio.Semaphore(2)
subtitle_slots = asyncio.Semaphore(1)
_subtitle_jobs = {}


def cover_path(library, item):
    source = source_path(library, item)
    root = safe_root(library["folder"])
    candidates = [source.with_suffix(ext) for ext in (".jpg", ".png", ".webp")]
    candidates += [source.parent / name for name in ("folder.png", "poster.jpg", "cover.jpg", "folder.jpg", "cover.png")]
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if root in resolved.parents and resolved.stat().st_size <= 20 * 1024 * 1024:
            return resolved
    return None


def folder_cover_path(library, relative):
    root = safe_root(library['folder'])
    parts = Path(relative)
    if parts.is_absolute() or '..' in parts.parts:
        raise ValueError('Invalid folder')
    target = root
    for part in parts.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('Linked folders are unavailable')
    if ignored_folder(root, target):
        raise ValueError('Ignored folder')
    image = target / 'folder.png'
    if image.is_symlink() or not image.is_file() or image.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('Folder artwork unavailable')
    image.resolve().relative_to(root)
    return image


@lru_cache(maxsize=128)
def cover_bytes(path, mtime_ns, size):
    """Small, stripped thumbnails from local artwork; no scraping or video decoding."""
    from PIL import Image, ImageOps
    with Image.open(path) as original:
        if original.width * original.height > 20_000_000:
            raise ValueError("Cover image is too large")
        thumb = ImageOps.exif_transpose(original)
        thumb.thumbnail((400, 400))
        output = BytesIO()
        thumb.convert("RGB").save(output, "JPEG", quality=80)
        return output.getvalue()


async def limits():
    return {**DEFAULT_LIMITS, **(await read("limits") or {})}


def allowed_profiles(config):
    return [name for name, (_, _, video, audio) in PROFILES.items()
            if (video + audio) * 1.2 <= config["viewer_kbps"]]


def touch_session(ticket, viewer, config):
    now = time.monotonic()
    for key, (_, seen) in list(_sessions.items()):
        if now - seen > 90:
            _sessions.pop(key, None)
    if ticket not in _sessions and len(_sessions) >= config["max_streams"]:
        raise RuntimeError("All Media Center stream slots are busy; try again shortly")
    _sessions[ticket] = (viewer, now)


@asynccontextmanager
async def transcode_slot(config):
    global _active_transcodes
    async with _job_condition:
        await asyncio.wait_for(_job_condition.wait_for(lambda: _active_transcodes < config["max_transcodes"]), 20)
        _active_transcodes += 1
    try:
        yield
    finally:
        async with _job_condition:
            _active_transcodes -= 1
            _job_condition.notify_all()


async def paced_bytes(data, viewer, config):
    """Pace actual response bytes; one budget per server and per Nostr identity.

    Small chunks bound bursts; all of a viewer's tabs share the same budget.
    State is process-local, matching the application's single ASGI worker.
    """
    for offset in range(0, len(data), 16384):
        chunk = data[offset:offset + 16384]
        while True:
            async with _rate_lock:
                now = time.monotonic()
                for key, due in list(_rate_due.items()):
                    if due < now - 120:
                        _rate_due.pop(key, None)
                delay = max(0, _rate_due.get("server", now) - now, _rate_due.get(viewer, now) - now)
                if delay <= 0:
                    _rate_due["server"] = now + len(chunk) * 8 / (config["server_kbps"] * 1000)
                    _rate_due[viewer] = now + len(chunk) * 8 / (config["viewer_kbps"] * 1000)
                    break
            # A viewer waiting on their own cap reserves no global bandwidth.
            # Other users can keep streaming, and disconnects leave no queued debt.
            await asyncio.sleep(delay)
        yield chunk


def identity(user):
    value = getattr(user, "nostr_npub", None)
    return normalize_pubkey(value) if value else ""


def normalize_pubkey(value):
    if not isinstance(value, str) or not (value.startswith("npub1") or re.fullmatch(r"[0-9a-fA-F]{64}", value)):
        raise ValueError("Use an npub or 64-character Nostr public key")
    key = to_pubkey_hex(value)
    if not key or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise ValueError("Invalid Nostr public key")
    return key.lower()


def resolve_share_key(value):
    """Accept the name a person is actually known by, not only the key they never see.

    Sharing took an npub or 64 hex characters and nothing else, so the owner — who knows the account
    as `matthew@poster.place`, granted it a NIP-05 name from this very node, and had just ticked its
    Media Center permission — had no way to type it. They used the profile permission instead, which
    grants the FEATURE and not the library, and the share silently never happened.

    A NIP-05 address is resolved ONLY against this node's own registry (`nostr_relay_nip05_names`),
    never against a profile's self-declared `nip05`: anyone may write that claim into their own
    kind-0, and honouring it here would let a stranger be handed somebody else's library by typing a
    name they do not hold. The registry is the half this node signs for.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("Enter an npub, a public key, or a name this server granted")
    if v.startswith("npub1") or re.fullmatch(r"[0-9a-fA-F]{64}", v):
        return normalize_pubkey(v)
    local = v.split("@", 1)[0].strip().lower()
    if not local or not re.fullmatch(r"[a-z0-9._-]{1,64}", local):
        raise ValueError("Use an npub, a 64-character public key, or a name this server granted")
    try:
        from app.services import settings_store
        from app.services.nostr_relay.thread import _parse_nip05
        names, _ = _parse_nip05(settings_store.get("nostr_relay_nip05_names", "") or "", "")
    except Exception:
        names = {}
    hit = {str(k).lower(): val for k, val in (names or {}).items()}.get(local)
    if not hit:
        raise ValueError("This server has not granted the name %r. Use their npub or public key."
                         % v)
    return str(hit).lower()


def can_read(library, pubkey):
    return bool(pubkey) and (library["owner"] == pubkey or pubkey in library.get("shared_with", []))


def roots():
    default = str(Path(os.environ.get("POSTERCHANAI_DATA", "/var/lib/posterchanai")) / "media")
    return [Path(p).resolve() for p in os.environ.get("POSTERCHANAI_MEDIA_ROOTS", default).split(os.pathsep) if p]


def safe_root(value):
    value = value.strip()
    if not value or not Path(value).is_absolute():
        raise ValueError("Enter an absolute folder path on the media server")
    try:
        path = Path(value).resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("This folder does not exist on the media server") from error
    except PermissionError as error:
        raise ValueError("The media server cannot access this folder") from error
    if not path.is_dir():
        raise ValueError("Select a folder, not a file")
    if not any(path == root or root in path.parents for root in roots()):
        raise ValueError("Folder is outside the allowed media roots shown in Add a server folder. "
                         "Configure POSTERCHANAI_MEDIA_ROOTS on the media server and restart it")
    return path


def source_path(library, item):
    root = safe_root(library["folder"])
    path = root / item["path"]
    if ignored_folder(root, path.parent):
        raise ValueError('This folder is excluded from Media Center')
    # Do not follow symlinks, including a directory swapped since scanning.
    if Path(item["path"]).is_absolute() or ".." in Path(item["path"]).parts:
        raise ValueError("Invalid media path")
    cursor = root
    for part in Path(item["path"]).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symbolic links are not media sources")
    resolved = path.resolve(strict=True)
    if root not in resolved.parents or not resolved.is_file():
        raise ValueError("Media file is outside its library")
    stat = resolved.stat()
    if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
        raise ValueError("Media changed; rescan the library")
    return resolved


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format",
                             "-show_streams", "-of", "json", str(path)], capture_output=True, timeout=20, check=True)
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0 or duration > 7 * 86400:
        raise ValueError("Unsupported media duration")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    tracks = []
    for stream in data.get('streams', []):
        if stream.get('codec_type') not in ('audio', 'subtitle'):
            continue
        tags = stream.get('tags', {})
        tracks.append({'index': stream['index'], 'type': stream['codec_type'],
                       'codec': stream.get('codec_name', ''), 'language': tags.get('language', 'und'),
                       'title': tags.get('title', '')[:150],
                       'default': bool(stream.get('disposition', {}).get('default')),
                       'forced': bool(stream.get('disposition', {}).get('forced')),
                       'text': stream.get('codec_name') in ('subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text')})
    return {"duration": duration, "video": bool(video), 'tracks': tracks}


@lru_cache(maxsize=128)
def cached_tracks(path, mtime_ns, size):
    return probe(path)['tracks']


def item_tracks(library, item):
    if 'tracks' in item:
        return item['tracks']
    path = source_path(library, item)
    stat = path.stat()
    return cached_tracks(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=32)
def subtitle_bytes(path, mtime_ns, size, index):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
                             '-discard:v', 'all', '-discard:a', 'all', '-discard:d', 'all',
                             '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', f'0:{index}',
                             '-c:s', 'webvtt', '-f', 'webvtt', 'pipe:1'],
                            capture_output=True, check=True, timeout=90)
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError('Subtitle track is too large')
    return result.stdout


async def subtitle_track(library, item, index):
    path = await asyncio.to_thread(source_path, library, item)
    stat = path.stat()
    key = (path, stat.st_mtime_ns, stat.st_size, index)
    job = _subtitle_jobs.get(key)
    if job is None:
        if len(_subtitle_jobs) >= 16:
            raise ValueError('Subtitle queue is full')
        async def extract():
            async with subtitle_slots:
                return await asyncio.to_thread(subtitle_bytes, *key)
        job = asyncio.create_task(extract())
        _subtitle_jobs[key] = job
        def finished(task):
            _subtitle_jobs.pop(key, None)
            if not task.cancelled():
                task.exception()
        job.add_done_callback(finished)
    return await asyncio.shield(job)


def natural(value):
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", value)]


def ignored_folder(root, folder, memo=None):
    """A .ignore marker excludes its entire subtree; memoize ancestors per listing."""
    memo = {} if memo is None else memo
    if folder in memo:
        return memo[folder]
    if folder != root and root not in folder.parents:
        return True
    ignored = (folder / '.ignore').exists() or (folder != root and ignored_folder(root, folder.parent, memo))
    memo[folder] = ignored
    return ignored


def visible_catalog(library, items):
    root = Path(library['folder'])
    memo = {}
    return [item for item in items if not ignored_folder(root, (root / item['path']).parent, memo)]


def generate_folder_art(root, source, metadata, attempted):
    """One bounded decode supplies missing ancestor artwork, without replacing user art."""
    if not metadata.get('video'):
        return
    targets = []
    folder = source.parent
    while folder == root or root in folder.parents:
        if folder not in attempted:
            attempted.add(folder)
            target = folder / 'folder.png'
            if not os.path.lexists(target) and os.access(folder, os.W_OK):
                targets.append(target)
        if folder == root:
            break
        folder = folder.parent
    if not targets:
        return
    try:
        result = subprocess.run([
            'ffmpeg', '-v', 'error', '-nostdin', '-threads', '1', '-filter_threads', '1',
            '-protocol_whitelist', 'file,pipe', '-ss', str(min(30, metadata['duration'] * .1)),
            '-i', str(source), '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=480:-2',
            '-c:v', 'png', '-threads', '1', '-f', 'image2pipe', 'pipe:1',
        ], capture_output=True, timeout=15, check=True)
        if not result.stdout.startswith(b'\x89PNG\r\n\x1a\n'):
            return
        for target in targets:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='.media-cover-', suffix='.png', dir=target.parent, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(result.stdout)
                temporary.chmod(0o644)
                os.link(temporary, target)  # Atomic and fails if artwork appeared during decoding.
            except OSError:
                pass  # Read-only mounts and existing images never fail a media scan.
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        pass


MAX_LIBRARY_ITEMS = 25_000


class LibraryLimitError(ValueError):
    pass


def scan(folder, previous=None, on_item=None):
    root = safe_root(folder)
    items, skipped = [], 0
    previous = {item["path"]: item for item in (previous or [])}
    artwork_attempted = set()
    def failed(error):
        raise error  # An unreadable subtree must not silently erase the old catalog.
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        if '.ignore' in files or '.ignore' in dirs:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if name.startswith(".") or path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                old = previous.get(relative)
                if old and old["size"] == stat.st_size and old["mtime_ns"] == stat.st_mtime_ns:
                    items.append(old)
                    if len(items) > MAX_LIBRARY_ITEMS:
                        raise LibraryLimitError(f"Library limit is {MAX_LIBRARY_ITEMS:,} items; split this folder into libraries")
                    if on_item:
                        on_item(old)
                    generate_folder_art(root, path, old, artwork_attempted)
                    continue
                metadata = probe(path)
                items.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:32], "path": relative,
                              "name": path.stem, "folder": path.parent.relative_to(root).as_posix(),
                              "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, **metadata})
                if on_item and len(items) <= MAX_LIBRARY_ITEMS:
                    on_item(items[-1])
                generate_folder_art(root, path, metadata, artwork_attempted)
            except (ValueError, OSError, subprocess.SubprocessError):
                skipped += 1
            if len(items) > MAX_LIBRARY_ITEMS:
                raise LibraryLimitError(f"Library limit is {MAX_LIBRARY_ITEMS:,} items; split this folder into libraries")
    items.sort(key=lambda item: (natural(item["folder"]), natural(item["path"])))
    return items, skipped


_progress_lock = asyncio.Lock()


def progress_key(library_id, viewer):
    return 'progress:' + library_id + ':' + hashlib.sha256(viewer.encode()).hexdigest()


async def playback_history(library_id, viewer):
    return await read(progress_key(library_id, viewer)) or {}


async def save_progress(library_id, viewer, item_id, position, duration):
    position = min(max(0.0, position), max(0.0, duration))
    played = duration > 0 and position >= duration - min(30, duration * .05)
    return await save_user_data(library_id, viewer, item_id,
                                {'position': 0 if played else round(position, 3), 'played': played})


async def save_user_data(library_id, viewer, item_id, update):
    async with _progress_lock:
        history = await playback_history(library_id, viewer)
        record = {**history.get(item_id, {'position': 0, 'played': False}), **update, 'updated': time.time()}
        history[item_id] = record
        # One bounded encrypted document per viewer/library, independent of app tokens.
        history = dict(sorted(history.items(), key=lambda entry: entry[1]['updated'], reverse=True)[:200])
        await write(progress_key(library_id, viewer), history)
    return record


async def read(key):
    return await nostr_store.get_doc(settings_store._port(), NS + key,
                                    seckey=settings_store._operator_seckey(None), strict=True)


# How long an account record is trusted without re-reading it, and how long a cached one may still
# answer once the relay has stopped answering.
ACCOUNT_FRESH = 30.0
ACCOUNT_STALE_OK = 600.0
_ACCOUNT_MAX = 256


async def read_account(key):
    """The account record behind a Jellyfin token — cached, because it is read PER REQUEST.

    A PLAYER FETCHES A SEGMENT EVERY TWO SECONDS, AND EVERY ONE OF THEM WAS A FRESH WEBSOCKET TO THE
    RELAY. `jellyfin.authenticate` reads this document to find the session that matches the token, so
    the read sat in front of every `.ts` in a stream — and `nostr_store._ws_query` opens its own
    socket per call. Reported as "jellyfin stopped playing twice"; measured in the journal as

        authenticate -> media_center.read -> nostr_store.get_doc -> _ws_query
        TimeoutError: timed out during opening handshake
        GET /jellyfin/Videos/<id>/480p-188.ts -> 500 Internal Server Error

    i.e. one slow websocket handshake ended somebody's film. Nothing was wrong with the media, the
    token or the session; the request simply could not open a socket in time, and an unhandled
    exception in an auth dependency is a 500 on a video segment.

    THE FAILURE MODE THIS FIXES IS "COULD NOT ASK", AND IT IS ANSWERED THE WAY THIS REPO ANSWERS IT
    EVERYWHERE ELSE: could-not-ask is never a denial. A record that was read successfully keeps
    answering for `ACCOUNT_STALE_OK` while the relay is unreachable, so a blip costs nobody their
    playback. That is a deliberate trade with a bound: a session revoked DURING a relay outage stays
    usable until the outage ends or ten minutes pass, whichever is first. Every other gate still runs
    per request against the database — `media_allowed`, the user lookup, `require_user` — so this
    caches WHICH SESSIONS EXIST, never whether the account may watch.

    A missing record is not cached: an account created a second ago must be able to log in.
    """
    now = time.time()
    hit = _account_cache.get(key)
    if hit and now - hit[1] < ACCOUNT_FRESH:
        return hit[0]
    try:
        record = await read(key)
    except Exception as error:
        if hit and now - hit[1] < ACCOUNT_STALE_OK:
            logger.warning("[media-center] account read failed (%s) — answering from the copy read "
                           "%.0fs ago rather than ending a session", type(error).__name__, now - hit[1])
            return hit[0]
        raise
    if record:
        if len(_account_cache) >= _ACCOUNT_MAX:
            _account_cache.pop(min(_account_cache, key=lambda k: _account_cache[k][1]), None)
        _account_cache[key] = (record, now)
    else:
        _account_cache.pop(key, None)
    return record


def forget_account(key):
    """Drop the cached record — called wherever sessions are written, so a login, a logout or a
    revocation takes effect at once instead of at the end of the freshness window."""
    _account_cache.pop(key, None)


async def write(key, value):
    if len(json.dumps(value, separators=(",", ":")).encode()) > 60000:
        raise ValueError("Media catalog document is too large; split this folder into smaller libraries")
    if not await nostr_store.put_doc(settings_store._port(), settings_store._operator_seckey(None), NS + key, value):
        raise RuntimeError("Media Center could not save its encrypted event")


async def libraries():
    index = await read("index") or {"ids": []}
    result = []
    for library_id in index["ids"]:
        library = await read("library:" + library_id)
        if library:
            result.append(library)
    return sorted(result, key=lambda lib: natural(lib["name"]))


async def catalog(library):
    cache_key = tuple(library.get("pages", []))
    if cache_key in _catalog_cache:
        return _catalog_cache[cache_key]
    result = []
    for key in library.get("pages", []):
        page = await read(key)
        if page is None:
            raise RuntimeError("Media catalog is incomplete; rescan the library")
        result.extend(page)
    if len(_catalog_cache) >= 8:
        _catalog_cache.pop(next(iter(_catalog_cache)))
    _catalog_cache[cache_key] = result
    return result


def encoder_candidates(mode):
    return {"auto": ["h264_nvenc", "h264_vaapi", "h264_amf", "libx264"],
            "nvidia": ["h264_nvenc", "libx264"], "amd": ["h264_vaapi", "h264_amf", "libx264"],
            "vaapi": ["h264_vaapi", "libx264"], "cpu": ["libx264"]}[mode]


def command(path, item, profile, number, encoder, output):
    width, height, bitrate, audio = PROFILES[profile]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "2", "-threads", "2"]
    if encoder == "h264_vaapi" and item["video"]:
        cmd += ["-vaapi_device", os.environ.get("POSTERCHANAI_MEDIA_VAAPI_DEVICE", "/dev/dri/renderD128")]
    cmd += ["-ss", str(number * SEGMENT), "-protocol_whitelist", "file,pipe", "-i", str(path),
            "-t", str(min(SEGMENT, item["duration"] - number * SEGMENT)), "-map", f"0:{item['_audio_stream']}" if item.get('_audio_stream', -1) >= 0 else "0:a:0?",
            "-map_metadata", "-1", "-sn", "-dn", "-c:a", "aac", "-ac", "2", "-b:a", f"{audio}k"]
    if item["video"]:
        scale = (f"scale=w='min(iw,{width})':h='min(ih,{height})':"
                 "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,format=yuv420p")
        if encoder == "h264_vaapi":
            scale += ",format=nv12,hwupload"
        if item.get('_subtitle_stream', -1) >= 0:
            cmd += ['-filter_complex_threads', '1', '-filter_complex',
                    f"[0:V:0][0:{item['_subtitle_stream']}]overlay=eof_action=pass:shortest=0,{scale}[subbed]",
                    '-map', '[subbed]']
        else:
            cmd += ['-map', '0:V:0', '-vf', scale]
        cmd += ["-c:v", encoder, "-b:v", f"{bitrate}k",
                "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k", "-g", "48", "-threads", "2"]
        if encoder == "libx264":
            cmd += ["-preset", "veryfast"]
    return cmd + ["-avoid_negative_ts", "make_zero", "-f", "mpegts", str(output)]


# Segments encoded AHEAD of the player. Every segment is made on demand by its own FFmpeg run, so
# without this each request paid the whole encode before its first byte: measured on nas (RTX 3060,
# 1080p HEVC 10-bit source) 1.2-1.4s per 6s segment, and up to 4.6s when a player's four parallel
# requests queued behind max_transcodes=2 — all of it inside a per-viewer byte budget that leaves a
# 480p stream only ~1.4x realtime to begin with. Encoding is ~4.5x realtime, so the next segments
# are ready long before they are asked for.
LOOKAHEAD = 2


def prefetch(library, item, profile, number, count, config):
    """Start encoding the next LOOKAHEAD segments into the cache, only on transcoder capacity that
    is idle right now. Never queues behind or ahead of a viewer: a request that arrives for one of
    these segments joins the running job (see `segment`), and nothing is started while every slot
    is busy or a viewer's job is waiting for one."""
    for ahead in range(number + 1, min(count, number + 1 + LOOKAHEAD)):
        if len(_segment_jobs) >= config["max_transcodes"]:  # running AND waiting jobs
            return
        key = _segment_key(library, item, profile, ahead)
        if key in _segment_jobs:
            continue
        _start_segment_job(key, library, item, profile, ahead, config, check_cache=True)


def _segment_key(library, item, profile, number):
    return json.dumps([library["folder"], item, profile, number, library["encoder"]], sort_keys=True)


def _start_segment_job(key, library, item, profile, number, config, check_cache=False):
    async def generate():
        if check_cache:
            cached = await asyncio.to_thread(cached_segment, library, item, profile, number)
            if cached is not None:
                return cached
        async with transcode_slot(config):
            return await asyncio.to_thread(transcode, library, item, profile, number, config)
    job = asyncio.create_task(generate())
    _segment_jobs[key] = job
    def finished(task):
        _segment_jobs.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve errors even if every viewer disconnected.
    job.add_done_callback(finished)
    return job


async def segment(library, item, profile, number, config):
    """Coalesce concurrent viewers before reserving a transcoder slot."""
    cached = await asyncio.to_thread(cached_segment, library, item, profile, number)
    if cached is not None:
        return cached
    key = _segment_key(library, item, profile, number)
    job = _segment_jobs.get(key)
    if job is None:
        if len(_segment_jobs) >= config["max_transcodes"] + config["max_streams"] * 2:
            raise RuntimeError("Media Center segment queue is full; retry shortly")
        job = _start_segment_job(key, library, item, profile, number, config)
    # A disconnected viewer cannot cancel work another viewer shares, or release
    # the job slot while the FFmpeg thread is still running.
    return await asyncio.shield(job)


def segment_cache_location(library, item, profile, number):
    """One validated source identity for both admission and encoder cache reads."""
    path = source_path(library, item)
    cache = Path(os.environ.get("POSTERCHANAI_MEDIA_CACHE", "/tmp/posterchan-media-center")).resolve()
    if Path("/tmp") not in cache.parents:
        raise ValueError("Media Center transcode cache must be a directory under /tmp")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([str(path), item, profile, number, library["encoder"], 1], sort_keys=True).encode()).hexdigest()
    target = cache / (key + ".ts")
    return path, cache, key, target


def cached_segment(library, item, profile, number):
    """Read completed data without waiting for an encoder or its striped lock."""
    import fcntl
    _, cache, _, target = segment_cache_location(library, item, profile, number)
    with (cache / ".cache-lock").open("a") as cache_lock:
        fcntl.flock(cache_lock, fcntl.LOCK_EX)
        if target.exists():
            os.utime(target, None)
            return target.read_bytes()
    return None


def transcode(library, item, profile, number, config=None):
    import fcntl
    path, cache, key, target = segment_cache_location(library, item, profile, number)
    config = config or DEFAULT_LIMITS
    # Fixed striped locks bound lock-file count. Different media encode concurrently;
    # identical segment requests wait and reuse the first result across processes.
    with (cache / (".lock-" + str(int(key[:8], 16) % 256))).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (cache / ".cache-lock").open("a") as cache_lock:
            fcntl.flock(cache_lock, fcntl.LOCK_EX)
            if target.exists():
                os.utime(target, None)
                return target.read_bytes()
        for encoder in encoder_candidates(library["encoder"]):
            if encoder != "libx264" and _failed_encoders.get(encoder, 0) > time.monotonic():
                continue
            with tempfile.NamedTemporaryFile(dir=cache, suffix=".part") as temp:
                started = time.monotonic()
                try:
                    # STDERR IS KEPT. Discarding it made every stutter unexplainable: the one
                    # component that knows which encoder ran, how long the segment took and why a
                    # hardware encoder was abandoned said nothing at all, on any log, ever.
                    subprocess.run(command(path, item, profile, number, encoder, temp.name),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   timeout=45 if encoder == "libx264" else 10, check=True)
                    took = time.monotonic() - started
                    # A segment is SEGMENT seconds of video. Taking longer than that to make means
                    # the stream cannot keep up, which is what the viewer experiences as a stutter
                    # before any error exists to find.
                    if took > SEGMENT:
                        logger.warning("[media] %s segment %s took %.1fs for %ss of video (%s)",
                                       encoder, number, took, SEGMENT, profile)
                    else:
                        logger.debug("[media] %s segment %s in %.1fs", encoder, number, took)
                    data = Path(temp.name).read_bytes()
                    if data:
                        with (cache / ".cache-lock").open("a") as cache_lock:
                            fcntl.flock(cache_lock, fcntl.LOCK_EX)
                            segments = sorted(cache.glob("*.ts"), key=lambda p: p.stat().st_mtime)
                            total = sum(p.stat().st_size for p in segments)
                            budget = config["cache_mb"] * 1024 * 1024
                            for old in segments:
                                if total + len(data) <= budget:
                                    break
                                total -= old.stat().st_size
                                old.unlink()
                            if len(data) <= budget:
                                # Publish the completed file atomically. A killed
                                # worker must never leave a partial cache hit.
                                # Both names are on the same /tmp filesystem;
                                # NamedTemporaryFile removes its name on exit.
                                os.link(temp.name, target)
                        return data
                except subprocess.TimeoutExpired:
                    # A TIMEOUT IS NOT A BROKEN ENCODER, and treating it as one is a latch set on a
                    # transient — the shape this codebase keeps rediscovering. This box shares its
                    # GPU with music and video generation, so one busy moment, one cold seek or one
                    # leaked NVENC session made a 6s segment miss a 10s budget ONCE and demoted
                    # every following segment to libx264 for five minutes. Measured from the
                    # outside: the player stepped 480p -> 360p and restarted its session three
                    # times in an hour, with nothing in any log.
                    #
                    # This segment still falls through to the next candidate, so the viewer gets
                    # their picture; what does not happen is a blanket ban on the hardware the box
                    # was bought for.
                    logger.warning("[media] %s timed out on segment %s (%ss of video) — falling "
                                   "back for this segment only", encoder, number, SEGMENT)
                    continue
                except (OSError, subprocess.SubprocessError) as exc:
                    # A real refusal — the encoder is missing, the device is gone, the driver said
                    # no. That IS worth remembering, and now it says what it was told.
                    detail = getattr(exc, "stderr", b"") or b""
                    if isinstance(detail, bytes):
                        detail = detail.decode("utf-8", "replace")
                    if encoder != "libx264":
                        _failed_encoders[encoder] = time.monotonic() + 300
                        logger.warning("[media] %s refused segment %s, not using it for 5 minutes: %s",
                                       encoder, number, " ".join(detail.split())[-300:] or exc)
                    else:
                        logger.error("[media] libx264 failed on segment %s: %s",
                                     number, " ".join(detail.split())[-300:] or exc)
                    continue
    raise RuntimeError("Transcoding failed, including CPU fallback; check FFmpeg and the media file")
