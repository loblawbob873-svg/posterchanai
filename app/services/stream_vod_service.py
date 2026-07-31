"""Save an ended live stream to the streamer's Blossom drive ("Past streams" VOD).

Recording is done by MediaMTX (see stream_service._write_config): while a stream is live it writes fmp4
segments into a temp dir — `<record_dir>/<token>/<timestamp>.mp4`. That dir is just a path (default /tmp);
mount it as tmpfs / point it at /dev/shm if you want RAM-backed recording with no SSD writes — the app
doesn't care which.

The publish token (and thus that dir) is **stable per user** across every stream (routers/streams._user_token),
so one dir accumulates every session's — and any crash-orphan's — segments. Clean, correctly-timed
per-session VODs come from an **authoritative go-live marker**:

  * The publish-auth hook (routers/streams.stream_auth) calls `mark_golive()`, dropping a `.golive` marker
    with the real session start (written only when absent, so a reconnect keeps the original). A dir with NO
    marker is not a tracked session — its files are crash-orphans and are only ever swept, never uploaded.
  * `claim()` uses the marker to stamp the VOD's start time and to take exactly this session's segments
    (mtime >= go-live; older orphans are left for the sweep — never re-claimed, since claim requires a
    marker and this session's marker is cleared once consumed). It atomically `os.rename`s them into a
    unique `<record_dir>/.jobs/<token>__<start>__<id>/` — the session boundary, the mutex, and a durable job.

Detection is trigger-independent and reconnect-safe: `process_pending()` (spawned by the stream-end reaper,
~30s) runs `_claim_ended_sessions()`, which for each marked dir whose newest segment has been idle for
`_END_QUIET_S` (past any OBS reconnect) AND which MediaMTX confirms is no longer publishing, claims the
session (or clears a stale marker that produced no footage). `_process()` then joins and COMPRESSES the
segments (`_prepare_upload` — the same `media_service.compress_video*` pass every other upload gets; the
raw source is what OBS happened to send, which is both huge and higher quality than any viewer was served)
and uploads — kill-switch + opt-in re-checked, concurrency-capped, and holding NO DB transaction across the
multi-GB transcode/hash/upload — then indexes a StreamVOD row, retrying failed jobs until `_MAX_JOB_AGE_S`.
Never raises into the end path.
"""
from __future__ import annotations

import asyncio
import glob
import itertools
import logging
import os
import shutil
import time
from typing import Optional

from app.database import SessionLocal
from app.models import User, StreamVOD
from app.services import blossom_service, stream_service, media_service, settings_store
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

# A segment untouched for this long is a completed recording, not one being actively written.
_QUIET_S = 12
# Newest segment must be idle at least this long before a dir is considered ended — longer than an OBS
# auto-reconnect gap, so the reaper can't claim mid-stream during a blip (mirrors the stream-end grace).
_END_QUIET_S = 60
# Clock/rounding tolerance between the go-live marker time and a segment's mtime.
_GOLIVE_SKEW = 5
# A `<token>/` file this old with no claim is a crash-orphan; swept at startup.
_STALE_S = 6 * 3600
# Give up on (delete) a staging job that still hasn't uploaded after this long (Blossom unreachable).
_MAX_JOB_AGE_S = 24 * 3600
# Cap concurrent VOD uploads so an outage-recovery backlog can't exhaust the DB pool / storage node.
_MAX_CONCURRENT_UPLOADS = 3
# Ceiling on the compression pass (see _prepare_upload). Generous because it scales with stream LENGTH and
# the fallback encoder is CPU libx264: a multi-hour stream can legitimately take hours. Overrunning is not
# a data-loss event — it falls back to the -c copy concat — but it must stay well under _MAX_JOB_AGE_S.
_COMPRESS_TIMEOUT_S = 6 * 3600

_JOBS_SUBDIR = ".jobs"
_GOLIVE_NAME = ".golive"
_counter = itertools.count()
_processing: set[str] = set()   # staging paths currently being processed
_tasks: set = set()             # strong refs to spawned _process tasks (event loop only weak-refs them)
_upload_sem = asyncio.Semaphore(_MAX_CONCURRENT_UPLOADS)
_pp_running = False             # process_pending re-entrancy guard (reaper may fire it while one runs)


def _rec_dir() -> str:
    return stream_service.recording_dir()


def _jobs_root() -> str:
    return os.path.join(_rec_dir(), _JOBS_SUBDIR)


def _safe_token(token: str) -> bool:
    return bool(token) and "/" not in token and "\\" not in token and token not in (".", "..")


def _golive_path(token: str) -> str:
    return os.path.join(_rec_dir(), token, _GOLIVE_NAME)


def mark_golive(token: str) -> None:
    """Record the authoritative session-start time at publish-auth. An existing marker is KEPT only if this
    is a reconnect of the same session (a segment was written recently); if the feed has been silent (a
    prior session ended/crashed without being claimed), the marker is stale and is replaced with this new
    go-live — so the next session never inherits a crashed session's start time."""
    if not _safe_token(token):
        return
    try:
        d = os.path.join(_rec_dir(), token)
        os.makedirs(d, exist_ok=True)
        marker = _golive_path(token)
        if os.path.exists(marker):
            newest = _newest_mtime(d)
            if newest is not None and time.time() - newest < _END_QUIET_S:
                return   # reconnect → preserve the original go-live
            # Not a reconnect. Don't blindly overwrite: an existing marker with settled footage is a
            # PRIOR session that ENDED but was never claimed (the reaper hasn't run yet). Overwriting it
            # with this later go-live would make claim() skip those older segments (mtime < new start),
            # orphaning them for the sweep and losing the recording. Finalize the prior session first —
            # claim() stages its segments and clears its marker — then write the fresh marker below. Only
            # a genuinely empty/stale marker (no footage belongs to it) is overwritten outright.
            prior = _golive_of(token)
            if prior is not None and any(os.path.getmtime(f) >= prior - _GOLIVE_SKEW
                                         for f in _quiet_segments(d)):
                claim(token)   # preserve the ended-but-unclaimed session; removes the old marker
        os.makedirs(d, exist_ok=True)   # claim() above can remove the emptied session dir — recreate it, or
        with open(marker, "w") as f:    # the new go-live marker never lands and THIS stream's recording is lost
            f.write(str(int(time.time())))
    except Exception as e:
        logger.debug("[stream-vod] mark_golive(%s) failed: %s", token[:8], e)


def _golive_of(token: str) -> Optional[float]:
    try:
        with open(_golive_path(token)) as f:
            return float(f.read().strip())
    except Exception:
        return None


def _remove_marker(token: str) -> None:
    try:
        os.remove(_golive_path(token))
    except OSError:
        pass


def _quiet_segments(session_dir: str) -> list[str]:
    """Completed fmp4 segments in a dir (glob('*.mp4') skips the dot-prefixed marker/concat artifacts)."""
    now = time.time()
    out = []
    for f in glob.glob(os.path.join(session_dir, "*.mp4")):
        try:
            if now - os.path.getmtime(f) >= _QUIET_S:
                out.append(f)
        except OSError:
            pass
    return out


def _newest_mtime(session_dir: str) -> Optional[float]:
    newest = None
    for f in glob.glob(os.path.join(session_dir, "*.mp4")):
        try:
            m = os.path.getmtime(f)
            if newest is None or m > newest:
                newest = m
        except OSError:
            pass
    return newest


def claim(token: str) -> Optional[str]:
    """Move this session's segments out of the shared `<token>/` dir into a unique staging dir and return
    it (or None). Requires a go-live marker; isolates the session by mtime (>= go-live), so a prior orphan
    is left behind, and — because claim requires a marker and clears it here — is never re-claimed."""
    if not _safe_token(token):
        return None
    start = _golive_of(token)
    if start is None:
        return None   # not a tracked session — orphan; leave for sweep, never a VOD
    session_dir = os.path.join(_rec_dir(), token)
    if not os.path.isdir(session_dir):
        return None
    files = [f for f in _quiet_segments(session_dir) if os.path.getmtime(f) >= start - _GOLIVE_SKEW]
    if not files:
        return None   # marker present but no settled segments belong to it (yet / never went live)
    started_at = int(start)
    staging = os.path.join(_jobs_root(), f"{token}__{started_at}__{int(time.time()*1000)}_{next(_counter)}")
    try:
        os.makedirs(staging, exist_ok=True)
        moved = 0
        for f in files:
            try:
                os.rename(f, os.path.join(staging, os.path.basename(f)))  # same tmpfs → atomic
                moved += 1
            except OSError:
                pass
        _remove_marker(token)             # session consumed
        _rmdir_if_empty(session_dir)
        if not moved:
            _rmdir_if_empty(staging)
            return None
        return staging
    except Exception as e:
        logger.warning("[stream-vod] claim failed for %s: %s", token[:8], e)
        return None


def _meta_of_staging(staging: str) -> tuple[str, int]:
    """(token, started_at) from the staging dir name `<token>__<started_at>__<uniq>` (token is hex)."""
    parts = os.path.basename(staging).split("__")
    token = parts[0] if parts else ""
    try:
        started_at = int(parts[1])
    except (IndexError, ValueError):
        started_at = 0
    return token, started_at


def _ffprobe_bin() -> str:
    """ffprobe next to the resolved ffmpeg (which may be a bundled path off $PATH), else the bare name."""
    try:
        cand = os.path.join(os.path.dirname(media_service.resolve_ffmpeg()), "ffprobe")
        if os.path.exists(cand):
            return cand
    except Exception:
        pass
    return "ffprobe"


def _concat_list(files: list[str], staging: str) -> str:
    """Write the ffmpeg concat-demuxer listing (dot-prefixed; never matched by the *.mp4 retry glob)."""
    listing = os.path.join(staging, ".concat.txt")
    with open(listing, "w") as f:
        for p in sorted(files):
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    return listing


async def _concat(files: list[str], staging: str) -> str:
    """Join fmp4 segments with -c copy (no re-encode). Output is dot-prefixed so a retry's *.mp4 glob
    never re-ingests it. Stays in the (tmpfs) staging dir, removed with it."""
    listing = _concat_list(files, staging)
    out = os.path.join(staging, ".joined.mp4")
    ff = media_service.resolve_ffmpeg()
    proc = await asyncio.create_subprocess_exec(
        ff, "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy",
        "-movflags", "+faststart", out,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"ffmpeg concat failed (rc={proc.returncode}): {err[-300:].decode('utf-8', 'replace')}")
    return out


async def _prepare_upload(files: list[str], staging: str) -> str:
    """Produce the single MP4 to upload: the session's segments joined AND compressed.

    Without this a VOD is whatever the streamer's encoder happened to send — a 2h 1080p60 @ 6 Mbps OBS
    stream is ~5.4 GB parked in that user's Blossom drive forever. It's also quality NOBODY EVER SAW:
    the bitrate clamp means viewers were served the 720p30 transcode, not the source. So the recording
    runs through the SAME pass every other upload gets (`media_service.compress_video*` — H.264/AAC,
    CRF, ≤1080p, NVENC → VAAPI → libx264 autodetect).

    Two things this deliberately does NOT do:
      * It does not concat first and then compress. The concat demuxer IS the compressor's input
        (`input_args`), so one pass reads the segments and writes the final file — concat-then-encode
        would materialise a full-size intermediate in a staging dir that is often tmpfs (RAM).
      * It does not take GPUResourceLock. Same reasoning as the live clamp: H.264 encoding runs on the
        GPU's media engine, separate silicon from the compute cores, so it doesn't contend with
        LLM/image/music/video generation — and a multi-hour stream would hold the lock for hours.
        Concurrency is already bounded by _upload_sem.

    Compression must never cost the user their recording, so ANY failure — every encoder failing, the
    timeout, or a result that came out BIGGER — falls back to the plain -c copy concat. That last case
    is real, not defensive padding: re-encoding an already-thin source inflates it (a 304 kbit/s phone
    publish measured 1447 kbit/s out through the live clamp), and a phone WHIP stream is exactly the
    kind of source that lands here.
    """
    raw = 0
    for f in files:
        try:
            raw += os.path.getsize(f)
        except OSError:
            pass
    out = os.path.join(staging, ".compressed.mp4")
    try:
        await asyncio.to_thread(
            media_service.compress_video_file,
            _concat_list(files, staging), out,
            input_args=["-f", "concat", "-safe", "0"],
            timeout=_COMPRESS_TIMEOUT_S,
        )
        size = os.path.getsize(out)
        if raw and size >= raw:
            logger.info("[stream-vod] compression inflated %s (%d → %d bytes) — keeping the original",
                        os.path.basename(staging), raw, size)
        else:
            logger.info("[stream-vod] compressed %s: %d → %d bytes", os.path.basename(staging), raw, size)
            return out
    except Exception as e:
        logger.warning("[stream-vod] compression failed for %s (%s) — uploading the original",
                       os.path.basename(staging), e)
    try:
        os.remove(out)
    except OSError:
        pass
    return files[0] if len(files) == 1 else await _concat(files, staging)


async def _duration_s(path: str) -> Optional[int]:
    """Best-effort length in seconds via ffprobe; None if unavailable (never fatal)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _ffprobe_bin(), "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return int(float(out.decode().strip())) if out.strip() else None
    except Exception:
        return None


def _rmdir_if_empty(d: str) -> None:
    try:
        os.rmdir(d)
    except OSError:
        pass


def _global_enabled() -> bool:
    if (settings_store.get("stream_record_enabled", "") or "").strip().lower() == "true":
        return True
    # The per-process settings cache can be stuck unhydrated (0 settings) in the reaper/worker process even
    # though the relay authoritatively holds stream_record_enabled=true — and when that happens EVERY ended
    # stream is silently dropped instead of saved. Before concluding recording is off, force a fresh
    # hydrate_from_db (reads the relay's Postgres directly — cheap + authoritative) and re-check. A flaky
    # cache must never cost the user their recordings.
    try:
        db = SessionLocal()
        try:
            settings_store.hydrate_from_db(db)
        finally:
            db.close()
    except Exception:
        pass
    return (settings_store.get("stream_record_enabled", "") or "").strip().lower() == "true"


def _backend_is_proxy() -> bool:
    backend = (settings_store.get("blossom_storage_backend", "") or "").strip().lower()
    storage_url = (settings_store.get("storage_server_url", "") or "").strip()
    return backend == "proxy" and bool(storage_url)


async def _process(staging: str) -> None:
    """Turn one claimed staging dir into a Blossom VOD. Kill-switch + opt-in gated, idempotent, and it
    swallows its own errors: on a transient failure the staging dir is LEFT for process_pending() to retry.
    Holds NO DB transaction across the multi-GB concat/hash/upload (would be killed at idle_in_transaction)."""
    if staging in _processing or not os.path.isdir(staging):
        return
    _processing.add(staging)
    try:
        files = sorted(glob.glob(os.path.join(staging, "*.mp4")))   # glob skips .joined.mp4
        if not files:
            shutil.rmtree(staging, ignore_errors=True)
            return
        token, started_at = _meta_of_staging(staging)

        # --- gating: a SHORT-LIVED read session, closed before the long work below ---
        db = SessionLocal()
        try:
            from app.services.stream_end_service import user_by_token
            user_id = user_by_token(db, token) if token else None
            user = db.query(User).filter(User.id == user_id).first() if user_id else None
            # user.stream_record is a LIVE Postgres read (not the settings cache), so it's authoritative.
            if not (user and getattr(user, "stream_record", False)):
                shutil.rmtree(staging, ignore_errors=True)   # opted out → discard
                return
            # Global kill-switch is the settings cache; a False may be an unhydrated transient at startup, so
            # DON'T delete — leave the job (it ages out at _MAX_JOB_AGE_S if recording really is off).
            if not _global_enabled():
                return
            pub = nostr_service.to_pubkey_hex(user.nostr_npub) if user.nostr_npub else None
            if not pub:
                logger.warning("[stream-vod] user %s opted in but has no usable nostr pubkey — leaving VOD "
                               "job %s for retry", user_id, os.path.basename(staging))
                return
            if db.query(StreamVOD).filter_by(token=token, started_at=started_at).first():
                shutil.rmtree(staging, ignore_errors=True)   # already saved this session
                return
        finally:
            db.close()   # release the connection BEFORE concat/hash/upload — no idle-in-transaction kill

        if not _backend_is_proxy():
            logger.warning("[stream-vod] Blossom backend is not a storage proxy — this VOD is written to "
                           "LOCAL disk (defeats the RAM→storage-node design). Configure a storage server.")

        async with _upload_sem:            # bound concurrent uploads + transcodes (pool / storage protection)
            out = await _prepare_upload(files, staging)
            duration = await _duration_s(out)
            db2 = SessionLocal()           # fresh session for the write phase (save_blob_file manages its txn)
            try:
                desc = await blossom_service.save_blob_file(db2, pub, out, "video/mp4")
                sha = desc.get("sha256")
                if not sha:
                    raise RuntimeError("Blossom upload returned no sha256")
                size = int(desc.get("size") or os.path.getsize(out))
                db2.add(StreamVOD(
                    user_id=user_id, pubkey=pub, token=token, sha256=sha, mime="video/mp4",
                    size=size, duration_s=duration, title=None,
                    started_at=started_at, created_at=int(time.time()),
                ))
                db2.commit()
            finally:
                db2.close()

        logger.info("[stream-vod] saved VOD for user %s (%s): %s, %d bytes, %ss",
                    user_id, token[:8], sha[:12], size, duration if duration is not None else "?")
        shutil.rmtree(staging, ignore_errors=True)   # success → drop the whole private job dir
    except Exception as e:
        logger.warning("[stream-vod] finalize failed for %s: %s (will retry)", os.path.basename(staging), e)
    finally:
        _processing.discard(staging)


async def _claim_ended_sessions() -> None:
    """Trigger-independent, reconnect-safe end detection: for each marked `<token>/` dir whose newest
    segment has been idle past any reconnect (_END_QUIET_S) AND that MediaMTX confirms is no longer
    publishing, claim the session (or clear a stale marker that produced no footage)."""
    try:
        rec = _rec_dir()
    except Exception:
        return
    if not os.path.isdir(rec):
        return
    from app.services.stream_end_service import is_publishing
    now = time.time()
    for token in os.listdir(rec):
        if token == _JOBS_SUBDIR:
            continue
        d = os.path.join(rec, token)
        if not os.path.isdir(d) or _golive_of(token) is None:
            continue   # only tracked (marked) sessions are ever claimed; others are orphans → sweep
        newest = _newest_mtime(d)
        if newest is not None and now - newest < _END_QUIET_S:
            continue   # a segment was written recently — still live or mid-reconnect
        try:
            live = await is_publishing(token)
        except Exception:
            continue
        if live is not False:
            continue   # True (live) or None (can't tell) → try again next pass
        if claim(token) is None:
            # claim() returned nothing. Clear the marker ONLY if it genuinely has no footage (announced but
            # never went live / only prior orphans) — NOT on a transient claim failure (e.g. a failed
            # rename), which must be left for the next pass to retry rather than silently dropped.
            start = _golive_of(token)
            if start is not None and not [f for f in _quiet_segments(d)
                                          if os.path.getmtime(f) >= start - _GOLIVE_SKEW]:
                _remove_marker(token)


async def process_pending() -> None:
    """One worker pass: detect & claim ended sessions, then (re)process staged jobs. Re-entrancy-guarded
    (the reaper may spawn it while a prior pass is still probing). Gives up on jobs older than
    _MAX_JOB_AGE_S; spawns each _process as its own task so a slow upload never blocks the pass."""
    global _pp_running
    if _pp_running:
        return
    _pp_running = True
    try:
        await _claim_ended_sessions()

        root = _jobs_root()
        if not os.path.isdir(root):
            return
        now = time.time()
        loop = asyncio.get_running_loop()
        for name in os.listdir(root):
            staging = os.path.join(root, name)
            if not os.path.isdir(staging) or staging in _processing:
                continue
            try:
                if now - os.path.getmtime(staging) > _MAX_JOB_AGE_S:
                    shutil.rmtree(staging, ignore_errors=True)
                    logger.warning("[stream-vod] gave up on stale VOD job %s (unuploadable > %dh)",
                                   name, _MAX_JOB_AGE_S // 3600)
                    continue
            except OSError:
                continue
            t = loop.create_task(_process(staging))
            _tasks.add(t)
            t.add_done_callback(_tasks.discard)
    finally:
        _pp_running = False


def sweep_orphans() -> None:
    """Startup: remove old files from MARKER-LESS `<token>/` dirs — true orphans with no tracked session.
    A dir WITH a `.golive` marker (a session, incl. one that ended while the app was down) is left for the
    worker to claim, so its marker/footage are never deleted out from under a pending finalize. .jobs/ is
    skipped (handled by process_pending)."""
    try:
        rec = _rec_dir()
    except Exception:
        return
    if not os.path.isdir(rec):
        return
    now = time.time()
    removed = 0
    for name in os.listdir(rec):
        if name == _JOBS_SUBDIR:
            continue
        d = os.path.join(rec, name)
        if not os.path.isdir(d):
            continue
        if _golive_of(name) is not None:
            # Tracked session — normally left for the worker to claim. But if nothing has been written for
            # a very long time (e.g. MediaMTX's control API was unreachable so is_publishing never returned
            # False → the session was never claimed), it's a leak. Only THEN fall through and remove it
            # (marker and all) so a stuck recording can't fill the temp dir indefinitely.
            nm = _newest_mtime(d)
            if nm is None or now - nm < _MAX_JOB_AGE_S:
                continue   # recent → still the worker's to claim; leave it
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) >= _STALE_S:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        _rmdir_if_empty(d)
    if removed:
        logger.info("[stream-vod] startup sweep removed %d orphaned recording file(s)", removed)
