"""Server media catalog and bounded, on-demand HLS transcoding.

Catalog/ACL documents are NIP-44 encrypted NIP-78 events, using the operator
storage key. Sharing is mediated by this server, not public relay publication.
"""
import asyncio
import hashlib
import json
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
                    if len(items) > 10000:
                        raise ValueError("Library limit is 10,000 items; split this folder into libraries")
                    if on_item:
                        on_item(old)
                    generate_folder_art(root, path, old, artwork_attempted)
                    continue
                metadata = probe(path)
                items.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:32], "path": relative,
                              "name": path.stem, "folder": path.parent.relative_to(root).as_posix(),
                              "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, **metadata})
                if on_item and len(items) <= 10000:
                    on_item(items[-1])
                generate_folder_art(root, path, metadata, artwork_attempted)
            except (ValueError, OSError, subprocess.SubprocessError):
                skipped += 1
            if len(items) > 10000:
                raise ValueError("Library limit is 10,000 items; split this folder into libraries")
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


async def segment(library, item, profile, number, config):
    """Coalesce concurrent viewers before reserving a transcoder slot."""
    key = json.dumps([library["folder"], item, profile, number, library["encoder"]], sort_keys=True)
    job = _segment_jobs.get(key)
    if job is None:
        if len(_segment_jobs) >= config["max_transcodes"] + config["max_streams"] * 2:
            raise RuntimeError("Media Center segment queue is full; retry shortly")
        async def generate():
            async with transcode_slot(config):
                return await asyncio.to_thread(transcode, library, item, profile, number, config)
        job = asyncio.create_task(generate())
        _segment_jobs[key] = job
        def finished(task):
            _segment_jobs.pop(key, None)
            if not task.cancelled():
                task.exception()  # Retrieve errors even if every viewer disconnected.
        job.add_done_callback(finished)
    # A disconnected viewer cannot cancel work another viewer shares, or release
    # the job slot while the FFmpeg thread is still running.
    return await asyncio.shield(job)


def transcode(library, item, profile, number, config=None):
    import fcntl
    path = source_path(library, item)
    cache = Path(os.environ.get("POSTERCHANAI_MEDIA_CACHE", "/tmp/posterchan-media-center")).resolve()
    if Path("/tmp") not in cache.parents:
        raise ValueError("Media Center transcode cache must be a directory under /tmp")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([str(path), item, profile, number, library["encoder"], 1], sort_keys=True).encode()).hexdigest()
    target = cache / (key + ".ts")
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
                try:
                    subprocess.run(command(path, item, profile, number, encoder, temp.name),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=45 if encoder == "libx264" else 10, check=True)
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
                except (OSError, subprocess.SubprocessError):
                    if encoder != "libx264":
                        _failed_encoders[encoder] = time.monotonic() + 300
                    continue
    raise RuntimeError("Transcoding failed, including CPU fallback; check FFmpeg and the media file")
