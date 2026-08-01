"""Startup janitor for temp files this app leaked by being KILLED.

Every temp path in this codebase is already wrapped in `try/finally: shutil.rmtree(...)`, and on
the normal paths — success and ordinary exceptions — that is enough. What `finally` cannot survive
is SIGKILL: an OOM kill, or a `sync.sh` restart landing mid-render, tears the process down without
running it, and whatever that render was holding stays in the temp dir forever.

On this deployment that is a RAM problem, not a disk one. `/tmp` is a 32 GB **tmpfs**, so a leaked
frame directory is pinned, unreclaimable memory — there is no swap, so those pages can never be
paged out. That makes the failure self-reinforcing: a kill leaks temp, leaked temp is RAM the
kernel cannot reclaim, which makes the next kill likelier. The 2026-08-01 OOM kill of the app left
143 MB of `media_frames_*` behind and it was still resident days later. The only backstop was
systemd-tmpfiles' `q /tmp 1777 root root 10d` — up to ten days of orphans held in RAM.

**Startup is the right hook, and it is sufficient.** Orphans are created by kills, and a kill is
always followed by a start. Nothing here runs on a timer, because nothing else produces orphans:
the normal paths clean up after themselves.

Modelled on `stream_vod_service.sweep_orphans()`, which does exactly this for stream recordings —
this is the same idea widened to the rest of the app's temp prefixes.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Every prefix the app and the bots hand to `tempfile.*` WITHOUT a `dir=` — i.e. everything that
# lands in the system temp dir. Kept as an explicit ALLOWLIST, never a `tmp*` glob: the temp dir is
# shared with every other program on the box (182 unattributable `tmp*` entries were sitting there
# when this was written), and a janitor that deletes files it cannot prove are ours is a far worse
# bug than the leak it fixes.
#
# `tests/test_temp_sweep.py` re-derives this list from the source with an AST scan and fails if the
# code grows a prefix that is not here — otherwise a new temp path silently escapes the sweep,
# which is exactly how the 15 unprefixed call sites went unnoticed. It also fails on a temp
# creation with NO prefix at all, because an unnamed `tmpXXXX` can never be attributed to us.
_APP_TEMP_PREFIXES: Tuple[str, ...] = (
    "chromeshot_",
    "client_compress_",
    "fahh_pad_",
    "ffshot_",
    "media_alpha_", "media_alphaclip_", "media_alphastill_", "media_audio_", "media_caption_",
    "media_char_", "media_clip_", "media_compress_", "media_concat_", "media_extract_",
    "media_frames_", "media_genvid_", "media_hava_", "media_motion_", "media_motionv_",
    "media_musicvid_", "media_muxaudio_", "media_outro_", "media_overlay_", "media_paudio_",
    "media_paudio_ss_", "media_recolor_", "media_slideshow_", "media_vmotion_",
    "memetalk_",
    "parallax_",
    # `pcai_` ones are the prefixes THIS change introduced, on call sites that previously had none.
    # They are namespaced where the obvious name would have been generic enough to collide with
    # another program's temp files (`tts_`, `stt_`, `doc_`, `char_`, `client_probe_`) — the one way
    # an allowlisted sweep can still do harm is by matching something that is not ours, and nothing
    # depended on these names yet, so the namespace was free. The pre-existing families above are
    # deliberately NOT renamed: that would be churn with no safety gain.
    "pcai_blossom_vid_", "pcai_char_frame_", "pcai_char_work_", "pcai_client_probe_",
    "pcai_doc_bmp_", "pcai_music_", "pcai_stt_", "pcai_tts_",
    "pcmeme-", "pcmemegif-", "pcmemesrc-",
    "postcard_",
    "talk_cmd_",
    "tg_music_", "tg_pin_", "tg_png_", "tg_ytdl_", "tg_ytdlv_send_", "tg_ytdlvideo_",
    "voice_cmd_", "voice_ref_",
    # `ytdl_` already covers the three below; they are listed anyway so this stays a 1:1 inventory
    # of what the code actually creates, which is what makes the drift test meaningful.
    "ytdl_", "ytdl_bytes_", "ytdl_mp3_", "ytdl_video_",
)

# Deliberately far longer than any render this app performs (the longest are video/music
# generation, minutes; `talk` caps its audio at TALK_MAX_DURATION = 30s). The sweep runs at
# startup, when nothing of ours is in flight, so the age gate is not protecting our own work —
# it is protecting anything ELSE on the box that happens to share one of these prefixes.
_MIN_AGE_S = 6 * 3600


def _stat_tree(path: str) -> Optional[Tuple[float, int]]:
    """(newest mtime anywhere under `path`, total bytes). None if it cannot be read.

    The newest mtime is taken over the whole tree, not from the top entry: a DIRECTORY's own mtime
    only moves when its immediate children change, so a long render writing deep inside one can
    look stale from the outside.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    newest, total = st.st_mtime, st.st_size
    if not os.path.isdir(path) or os.path.islink(path):
        return newest, total
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            total += st.st_size
    return newest, total


def sweep_temp_orphans(min_age_s: float = _MIN_AGE_S, tmpdir: Optional[str] = None) -> int:
    """Delete allowlisted temp entries older than `min_age_s`. Returns how many were removed.

    `tmpdir` defaults to the system temp dir and exists so the tests can point this at a scratch
    directory. They must NOT do that by patching `tempfile.gettempdir`: this module holds the real
    stdlib module, so assigning to it swaps the function out process-wide, and the whole suite runs
    in one process — every unrelated temp path created while those tests ran would be redirected.

    Never raises: a janitor that can break startup is not worth having.
    """
    try:
        tmpdir = tmpdir or tempfile.gettempdir()
        with os.scandir(tmpdir) as it:
            entries = [e for e in it if e.name.startswith(_APP_TEMP_PREFIXES)]
    except Exception:
        logger.debug("[temp-sweep] could not read the temp dir", exc_info=True)
        return 0

    now = time.time()
    removed, freed, kept = 0, 0, 0
    for entry in entries:
        path = os.path.join(tmpdir, entry.name)
        try:
            stat = _stat_tree(path)
            if stat is None:
                continue
            newest, size = stat
            if now - newest < min_age_s:
                kept += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)                    # a symlink is unlinked, never followed
            removed += 1
            freed += size
        except OSError:
            logger.debug("[temp-sweep] could not remove %s", path, exc_info=True)

    if removed:
        logger.info("[temp-sweep] removed %d orphaned temp entr%s (%.1f MB) left by a previous "
                    "kill; %d still recent", removed, "y" if removed == 1 else "ies",
                    freed / 1048576.0, kept)
    return removed
