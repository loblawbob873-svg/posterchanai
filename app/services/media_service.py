"""
Media Service - Generic image/video compression and image<->PDF conversion.

Provides backend-agnostic helpers used by the Telegram and web chat
interfaces for the `compress` and `convert` commands:

  - compress_image / compress_video : shrink a single file's size
  - images_to_pdf / pdf_to_images   : convert between images and PDF

The high-level `compress_attachments` / `convert_attachments` take the common
(filename, data, content_type) attachment tuples used across the interfaces and
return a list of output-file dicts: {"filename", "data", "content_type"}.

Images use Pillow, PDF uses PyMuPDF (fitz), video uses the system ffmpeg binary
(same dependency already used by the thumbnail/transcode services).
"""
import io
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# File-type detection (mirrors thumbnail_service conventions)
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif', '.heic', '.heif'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp', '.ogv', '.mpeg', '.mpg'}

# Image compression defaults
IMAGE_MAX_DIMENSION = 2048   # downscale longest edge to this before re-encoding
IMAGE_JPEG_QUALITY = 70

# Video compression defaults (more aggressive than the web-playback transcode)
VIDEO_CRF = 28
VIDEO_PRESET = 'fast'
VIDEO_MAX_RESOLUTION = (1920, 1080)
VIDEO_AUDIO_BITRATE = '96k'

# Clip keeps the source resolution (it's a trim, not a shrink) so use a higher
# quality target than the compression path.
CLIP_CRF = 23

# Cap how many PDF pages we rasterize at once to bound memory and output count.
PDF_MAX_PAGES = 50

# A single output file produced by a media operation.
OutputFile = dict  # {"filename": str, "data": bytes, "content_type": str}


def _ext(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def detect_mime(data: bytes) -> tuple[str, str]:
    """(mime, suggested filename) sniffed from magic bytes; falls back to image/jpeg.

    A generic byte sniff with nothing platform-specific in it. It used to live beside one
    uploader simply because that was the first caller to need it; it belongs here.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image.jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image.png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image.gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    if data[4:8] == b"ftyp":
        return "video/mp4", "video.mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", "video.webm"
    return "image/jpeg", "image.jpg"


def is_image(filename: str, content_type: Optional[str] = None) -> bool:
    if content_type and content_type.startswith("image/"):
        return True
    return _ext(filename) in IMAGE_EXTENSIONS


def is_video(filename: str, content_type: Optional[str] = None) -> bool:
    if content_type and content_type.startswith("video/"):
        return True
    return _ext(filename) in VIDEO_EXTENSIONS


def is_pdf(filename: str, content_type: Optional[str] = None) -> bool:
    if content_type == "application/pdf":
        return True
    return _ext(filename) == ".pdf"


# AVIF/HEIC stills are ISOBMFF, so ffmpeg demuxes them with mov,mp4,m4a,3gp,3g2,mj2 — a demuxer
# with NO `loop` option. `-loop 1 -i photo.avif` therefore does not degrade to a still: ffmpeg
# aborts the whole command with "Option loop not found" (the same failure the GIF demuxer caused in
# meme_builder_service, where `-ignore_loop 0` is the per-format spelling). mov has no such
# spelling, and unlooped a single-frame input only covers its own frame duration, so the layer/clip
# would flash and vanish. Transcode the still to PNG once, up front, and loop THAT.
_ISOBMFF_STILL_EXTS = (".avif", ".avifs", ".heic", ".heif", ".hif")


def loopable_still(path: str) -> str:
    """Path to a still image that `-loop 1` can actually read — `path` itself for the ordinary
    formats, or a PNG written beside it for the ISOBMFF ones (AVIF/HEIC, i.e. what a phone or a
    modern browser hands you). Decoding is Pillow's, not ffmpeg's: Pillow ≥ 11.3 reads AVIF and
    pillow_heif covers HEIC even where this ffmpeg build cannot. Falls back to the original path if
    the conversion fails — an unreadable source is the caller's error to report, not ours to mask.
    An ANIMATED avif collapses to its first frame; a still render beats a failed one."""
    if not str(path).lower().endswith(_ISOBMFF_STILL_EXTS):
        return path
    try:
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except Exception:
            pass
        out = f"{path}.png"
        with Image.open(path) as im:
            ImageOps.exif_transpose(im).convert("RGBA").save(out, "PNG")
        return out
    except Exception as e:
        logger.warning(f"loopable_still: {path} could not be converted to PNG ({e}); using as-is")
        return path


def is_animated_gif(filename: str, data: bytes, content_type: Optional[str] = None) -> bool:
    """True for a multi-frame GIF. Such 'movie' GIFs must be compressed as video —
    treating them as still images flattens them to a single frame."""
    if _ext(filename) != ".gif" and content_type != "image/gif":
        return False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1
    except Exception:
        return False


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def parse_timecode(value: str) -> Optional[float]:
    """Parse a user-supplied time into seconds, or None if unparseable.

    Accepts plain seconds ("90", "12.5"), "M:SS", or "H:MM:SS" (each part may
    carry a fractional component, e.g. "1:30.5"). Negative values are rejected.
    """
    s = (value or "").strip()
    if not s:
        return None
    try:
        if ":" in s:
            parts = s.split(":")
            if len(parts) > 3:
                return None
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + float(part)
        else:
            seconds = float(s)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _fmt_time(seconds: Optional[float]) -> str:
    """Render seconds as H:MM:SS / M:SS for summaries ('end' if None)."""
    if seconds is None:
        return "end"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_FFMPEG_BIN = None  # cached resolved ffmpeg path


def _ffmpeg_has_libx264(path: str) -> bool:
    try:
        out = subprocess.run([path, '-hide_banner', '-encoders'],
                             capture_output=True, timeout=10)
        return b'libx264' in out.stdout
    except Exception:
        return False


def resolve_ffmpeg() -> str:
    """Resolve an ffmpeg binary that can actually encode H.264.

    The service PATH can be polluted — e.g. Intel oneAPI's setvars.sh prepends a
    conda-forge ffmpeg with no libx264, which rejects `-preset`. So we prefer a
    known full build (the Jellyfin/system ffmpeg) and verify libx264 support
    rather than trusting whatever bare `ffmpeg` resolves to. Result is cached.
    """
    global _FFMPEG_BIN
    if _FFMPEG_BIN is not None:
        return _FFMPEG_BIN
    candidates = [
        os.environ.get("FFMPEG_BINARY"),
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        shutil.which("ffmpeg"),
    ]
    # Prefer a build that has the libx264 encoder.
    for c in candidates:
        if c and os.path.exists(c) and _ffmpeg_has_libx264(c):
            _FFMPEG_BIN = c
            return c
    # Fall back to any ffmpeg we can find (video compression may then be limited).
    for c in candidates:
        if c and os.path.exists(c):
            _FFMPEG_BIN = c
            return c
    _FFMPEG_BIN = "ffmpeg"
    return _FFMPEG_BIN


def compress_audio_opus(data: bytes, bitrate: str = "96k") -> bytes:
    """Transcode arbitrary audio bytes to Opus in an Ogg container at `bitrate` (default 96k — great
    quality-per-byte for music, e.g. a 50 MB WAV → ~3-4 MB) via the system ffmpeg, to save storage and
    bandwidth. Returns the compressed bytes. Raises on failure."""
    ff = resolve_ffmpeg()
    fd, inp = tempfile.mkstemp(prefix="media_audio_", suffix=".audio")
    os.close(fd)
    out = inp + ".ogg"
    try:
        with open(inp, "wb") as f:
            f.write(data)
        # bitexact on the OUTPUT (fixes the random Ogg serial + drops the encoder version string) +
        # strip metadata → DETERMINISTIC output: same audio → same bytes → same encrypted hash → Blossom
        # dedups a re-uploaded library. (Must be an OUTPUT flag — the serial is set by the muxer.)
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", inp,
                        "-vn", "-c:a", "libopus", "-b:a", bitrate, "-application", "audio",
                        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", out],
                       check=True, capture_output=True, timeout=600)
        with open(out, "rb") as f:
            return f.read()
    finally:
        for p in (inp, out):
            try:
                os.remove(p)
            except OSError:
                pass


def ffmpeg_available() -> bool:
    try:
        subprocess.run([resolve_ffmpeg(), '-version'], capture_output=True, timeout=5, check=True)
        return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Image compression
# ---------------------------------------------------------------------------

def compress_image(
    data: bytes,
    max_dimension: int = IMAGE_MAX_DIMENSION,
    quality: int = IMAGE_JPEG_QUALITY,
) -> bytes:
    """Re-encode an image as a downscaled JPEG to reduce file size.

    Raises on failure so callers can report a meaningful error.
    """
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        # Respect EXIF orientation, then drop EXIF (smaller, no rotation surprises).
        img = ImageOps.exif_transpose(img)

        # Flatten transparency/palette onto white for JPEG output.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Downscale the longest edge if the image is larger than the limit.
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
        return out.getvalue()


# ---------------------------------------------------------------------------
# Video compression
# ---------------------------------------------------------------------------

_video_encoder_cache = None  # remembers the first encoder that worked


def _ffmpeg_encoders_text(ffmpeg: str) -> str:
    try:
        out = subprocess.run([ffmpeg, '-hide_banner', '-encoders'], capture_output=True, timeout=10)
        return out.stdout.decode('utf-8', 'ignore')
    except Exception:
        return ""


def _render_node() -> str:
    import glob
    nodes = sorted(glob.glob('/dev/dri/renderD*'))
    return nodes[0] if nodes else '/dev/dri/renderD128'


def _video_encoder_candidates(ffmpeg: str) -> list:
    """Ordered list of H.264 encoders to try: GPU (if present) first, libx264 last.

    Smart-detects NVIDIA NVENC and Intel/AMD VAAPI from the available encoders +
    device nodes. Overridable with VIDEO_ENCODER=…; disable HW with VIDEO_HWACCEL=0.
    """
    import glob
    forced = os.environ.get("VIDEO_ENCODER", "").strip()
    if forced:
        return [forced] if forced == "libx264" else [forced, "libx264"]
    if os.environ.get("VIDEO_HWACCEL", "1").lower() in ("0", "false", "no"):
        return ["libx264"]

    enc = _ffmpeg_encoders_text(ffmpeg)
    cands = []
    if "h264_nvenc" in enc and glob.glob("/dev/nvidia*"):
        cands.append("h264_nvenc")                       # NVIDIA (e.g. nas.lan)
    if "h264_vaapi" in enc and glob.glob("/dev/dri/renderD*"):
        cands.append("h264_vaapi")                       # Intel Arc / AMD (render node)
    cands.append("libx264")                              # CPU fallback (always)
    return cands


_SDR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
_HDR_TRC = {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}


def _probe_color(ffmpeg: str, in_path: str) -> dict:
    """What colour space and bit depth the SOURCE is in. `{}` when it cannot be read."""
    probe = (ffmpeg[:-6] + "ffprobe") if ffmpeg.endswith("ffmpeg") else "ffprobe"
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=pix_fmt,color_transfer,color_primaries,color_space",
             "-of", "default=nw=1", in_path],
            capture_output=True, timeout=20)
        got = {}
        for line in out.stdout.decode("utf-8", "ignore").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                got[k.strip()] = v.strip()
        return got
    except Exception:
        return {}


def _is_hdr_or_10bit(info: dict) -> bool:
    pix = (info.get("pix_fmt") or "").lower()
    return bool(
        (info.get("color_transfer") or "").lower() in _HDR_TRC
        or (info.get("color_space") or "").lower().startswith("bt2020")
        or (info.get("color_primaries") or "").lower().startswith("bt2020")
        or "10le" in pix or "10be" in pix or "12le" in pix or "12be" in pix or "p010" in pix
    )


def _sdr_filter(info: dict, have_zscale: bool) -> str:
    """Bring HDR / 10-bit video down to something every player can actually decode.

    Two different jobs, and conflating them is what made the first attempt fail outright:

      * 10-BIT is a DEPTH problem. `format=yuv420p` is the whole fix, and it applies to every source
        here — it is what stops libx264 emitting High 10, which is the half that will not play.
      * HDR is a TRANSFER problem, and only tonemapping fixes it. `tonemap` works in LINEAR light, so
        zscale has to convert in and back out to BT.709 around it; running it on PQ/HLG values
        directly gives the washed-out result the filter exists to prevent. `desat=0` keeps
        highlights from going grey.

    `tin=` IS LOAD-BEARING. zscale cannot infer a transfer it was not told, and a stream whose tags
    ffmpeg reports as `unknown` — which plenty of real files are — makes a bare `t=linear` fail with
    EINVAL and take the ENTIRE encode with it: no video at all, which is worse than the bug. So the
    tonemap is only built when the source states a transfer we recognise, and everything else falls
    through to the depth fix, which is always safe and never errors.
    """
    trc = (info.get("color_transfer") or "").lower()
    if have_zscale and trc in ("smpte2084", "arib-std-b67"):
        return (f"zscale=tin={trc}:t=linear:npl=100,tonemap=tonemap=hable:desat=0,"
                "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p")
    return "format=yuv420p"


_zscale_cache = None


def _have_zscale(ffmpeg: str) -> bool:
    global _zscale_cache
    if _zscale_cache is None:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, timeout=10)
            _zscale_cache = " zscale " in out.stdout.decode("utf-8", "ignore")
        except Exception:
            _zscale_cache = False
    return _zscale_cache


def _video_encode_cmd(ffmpeg, encoder, in_path, out_path, scale_filter, crf, preset, input_args=None):
    """Build the ffmpeg command for a specific H.264 encoder.

    `input_args` goes immediately before `-i` for callers whose input isn't a plain file — the only
    user today is the concat demuxer (`-f concat -safe 0`), which lets a multi-segment live-stream
    recording be joined and compressed in ONE pass instead of writing a full-size intermediate.
    """
    """
    HDR AND 10-BIT ARE FORCED DOWN TO 8-BIT BT.709, AND THAT IS NOT A QUALITY CHOICE.

    ffmpeg follows its input: hand it the 10-bit HLG/PQ video every recent iPhone and Android
    records by default and libx264 encodes H.264 **High 10**, carrying the source's bt2020 tags
    onto the result. Both halves are fatal, and they are the two things people report:

      * High 10 AVC is not decodable by Chrome, Safari, Android MediaCodec or iOS. The upload
        succeeds, the post looks fine, and the video simply **will not play** for anybody.
      * Where something does decode it, BT.2020/HLG content rendered as if it were BT.709 crushes
        the blacks and blows the highlights — reported, exactly, as **"super high contrast"**.

    Measured, not reasoned about: before this, a 10-bit bt2020nc clip through compress_video_file
    came out `profile=High 10, pix_fmt=yuv420p10le, color_space=bt2020nc` — i.e. unchanged in every
    way that mattered and smaller in the one that did not. See tests/test_video_hdr_tonemap.py.

    So an HDR/10-bit source is tonemapped and retagged, and the output is ALWAYS 8-bit with explicit
    BT.709 tags. An ordinary SDR clip probes as SDR and its command is unchanged.
    """
    info = _probe_color(ffmpeg, in_path)
    hdr = _is_hdr_or_10bit(info)
    pre, vf = [], scale_filter
    if hdr:
        vf = scale_filter + ',' + _sdr_filter(info, _have_zscale(ffmpeg))
    if encoder == "h264_nvenc":
        venc = ['-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', str(crf)]
    elif encoder == "h264_vaapi":
        pre = ['-vaapi_device', _render_node()]
        # The tonemap is CPU work and must happen BEFORE the frame is uploaded to a GPU surface.
        vf = vf + ',format=nv12,hwupload'
        venc = ['-c:v', 'h264_vaapi', '-qp', str(crf)]
    elif encoder == "h264_amf":
        venc = ['-c:v', 'h264_amf', '-rc', 'cqp', '-qp_i', str(crf), '-qp_p', str(crf)]
    else:  # libx264
        venc = ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf)]
    # nv12 IS 8-bit, so VAAPI needs no -pix_fmt; naming one there fights the hwupload chain.
    depth = [] if encoder == "h264_vaapi" else ['-pix_fmt', 'yuv420p']
    return [ffmpeg] + pre + list(input_args or []) + ['-i', in_path, '-vf', vf] + venc + depth + _SDR_TAGS + [
        '-c:a', 'aac', '-b:a', VIDEO_AUDIO_BITRATE,
        '-movflags', '+faststart', '-y', out_path,
    ]


def compress_video_file(
    in_path: str,
    out_path: str,
    crf: int = VIDEO_CRF,
    preset: str = VIDEO_PRESET,
    max_resolution: Tuple[int, int] = VIDEO_MAX_RESOLUTION,
    input_args=None,
    timeout: int = 3600,
) -> str:
    """Compress a video FILE→FILE (H.264/AAC, downscaled). Returns `out_path`.

    This is the actual compression pass; `compress_video` is a bytes-in/bytes-out wrapper over it.
    Multi-GB inputs (a live-stream recording) must use this path — the bytes API holds the whole
    file, its temp copy AND the result in memory, which for a 2h 1080p60 stream is several GB each.

    Uses GPU acceleration when available (NVENC on NVIDIA, VAAPI on Intel Arc/AMD) and falls back to
    libx264 (CPU) if the GPU encoder is unavailable or fails. Raises RuntimeError if ffmpeg is
    unavailable or every encoder fails. Blocking — call it via asyncio.to_thread from async code.
    """
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")

    max_w, max_h = max_resolution
    # Scale down to fit within max resolution, keep aspect ratio, even dims.
    scale_filter = (
        f"scale='min({max_w},iw)':'min({max_h},ih)':"
        f"force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )

    candidates = _video_encoder_candidates(ffmpeg)
    # Try the previously-working encoder first to avoid re-failing GPU probes.
    if _video_encoder_cache and _video_encoder_cache in candidates:
        candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

    last_err = ""
    for encoder in candidates:
        cmd = _video_encode_cmd(ffmpeg, encoder, in_path, out_path, scale_filter, crf, preset, input_args)
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            if encoder != "libx264":
                logger.info(f"Video compressed with GPU encoder: {encoder}")
            _video_encoder_cache = encoder
            return out_path
        last_err = (result.stderr or "")[-300:]
        logger.warning(f"Video encoder {encoder} failed, trying next: {last_err}")
        if os.path.exists(out_path):
            os.unlink(out_path)

    raise RuntimeError(f"video compression failed (tried {candidates}): {last_err}")


def compress_video(
    data: bytes,
    source_filename: str,
    crf: int = VIDEO_CRF,
    preset: str = VIDEO_PRESET,
    max_resolution: Tuple[int, int] = VIDEO_MAX_RESOLUTION,
) -> bytes:
    """Compress a video with ffmpeg (H.264/AAC, downscaled). Returns MP4 bytes.

    Thin bytes wrapper over `compress_video_file` — see there for the encoder fallback chain. For a
    file already on disk (especially a large one) call that directly instead.
    """
    tmp_dir = tempfile.mkdtemp(prefix="media_compress_")
    in_suffix = _ext(source_filename) or ".mp4"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(data)
        compress_video_file(in_path, out_path, crf=crf, preset=preset, max_resolution=max_resolution)
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Video clipping (trim a [start, end] span — same ffmpeg/HW-accel path as compress)
# ---------------------------------------------------------------------------

def _clip_encode_cmd(ffmpeg, encoder, in_path, out_path, start, duration, crf):
    """Build an ffmpeg command that trims [start, start+duration] and re-encodes.

    Re-encodes (rather than stream-copying) so the cut is frame-accurate and the
    output is always a clean, faststart MP4. Resolution is preserved — this is a
    trim, not a downscale. Uses the same GPU encoders as compression when present.
    `-ss` is placed before `-i` for a fast keyframe seek; `-t` bounds the length.
    """
    seek = ['-ss', f'{start}']
    pre, vf = [], None
    if encoder == "h264_nvenc":
        venc = ['-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', str(crf)]
    elif encoder == "h264_vaapi":
        pre = ['-vaapi_device', _render_node()]
        vf = 'format=nv12,hwupload'                       # CPU decode → upload to GPU surface
        venc = ['-c:v', 'h264_vaapi', '-qp', str(crf)]
    elif encoder == "h264_amf":
        venc = ['-c:v', 'h264_amf', '-rc', 'cqp', '-qp_i', str(crf), '-qp_p', str(crf)]
    else:  # libx264
        venc = ['-c:v', 'libx264', '-preset', VIDEO_PRESET, '-crf', str(crf)]
    cmd = [ffmpeg] + pre + seek + ['-i', in_path]
    if duration is not None:
        cmd += ['-t', f'{duration}']
    if vf:
        cmd += ['-vf', vf]
    cmd += venc + ['-c:a', 'aac', '-b:a', VIDEO_AUDIO_BITRATE, '-movflags', '+faststart', '-y', out_path]
    return cmd


def clip_video(
    data: bytes,
    source_filename: str,
    start: float,
    end: Optional[float] = None,
    crf: int = CLIP_CRF,
) -> bytes:
    """Trim a video to the [start, end] span and return MP4 bytes.

    `start`/`end` are seconds (end=None clips to the end of the video). Uses GPU
    acceleration when available (NVENC / VAAPI), falling back to libx264 (CPU),
    exactly like compress_video. Raises on bad times or if every encoder fails.
    """
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    if start is None or start < 0:
        raise ValueError("start time is required")
    if end is not None and end <= start:
        raise ValueError("end time must be after the start time")
    duration = None if end is None else (end - start)

    tmp_dir = tempfile.mkdtemp(prefix="media_clip_")
    in_suffix = _ext(source_filename) or ".mp4"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(data)

        candidates = _video_encoder_candidates(ffmpeg)
        # Reuse the encoder that worked last time to skip re-probing dead GPUs.
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            cmd = _clip_encode_cmd(ffmpeg, encoder, in_path, out_path, start, duration, crf)
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                if encoder != "libx264":
                    logger.info(f"Video clipped with GPU encoder: {encoder}")
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"Clip encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"video clip failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def clip_attachment(
    attachments: List[Tuple[str, bytes, str]],
    start: float,
    end: Optional[float] = None,
) -> Tuple[List[OutputFile], str]:
    """Clip the first video attachment to [start, end].

    Returns (output_files, summary_text). Mirrors compress_attachments so the
    web UI and Telegram share one delivery path.
    """
    videos = [(fn, data, ct) for fn, data, ct in (attachments or []) if is_video(fn, ct)]
    if not videos:
        return [], "No video to clip — attach a video file first."

    filename, data, content_type = videos[0]
    stem = Path(filename).stem or "video"
    try:
        clipped = clip_video(data, filename, start, end)
        out: OutputFile = {
            "filename": f"{stem}_clip.mp4",
            "data": clipped,
            "content_type": "video/mp4",
        }
        span = f"{_fmt_time(start)}–{_fmt_time(end)}"
        summary = (
            f"## ✂️ Clip\n\n"
            f"🎬 {filename}: {span} → {_human_size(len(clipped))}"
        )
        return [out], summary
    except Exception as e:
        logger.error(f"clip failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Extract audio from a video  +  circle-crop an image  (upload-action commands)
# ---------------------------------------------------------------------------

def extract_audio(data: bytes, source_filename: str = "video", fmt: str = "mp3") -> bytes:
    """Pull the audio track out of a video into an MP3 (libmp3lame, VBR ~q2). Raises on failure
    (e.g. the video has no audio stream — callers report it per-file)."""
    ff = resolve_ffmpeg()
    suffix = Path(source_filename).suffix or ".video"
    fd, inp = tempfile.mkstemp(prefix="media_extract_", suffix=suffix)
    os.close(fd)
    out = inp + "." + fmt
    try:
        with open(inp, "wb") as f:
            f.write(data)
        if not _probe_has_audio(inp):
            raise ValueError("no audio track in this video")
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", inp,
                        "-vn", "-c:a", "libmp3lame", "-q:a", "2", out],
                       check=True, capture_output=True, timeout=600)
        with open(out, "rb") as f:
            return f.read()
    finally:
        for p in (inp, out):
            try:
                os.remove(p)
            except OSError:
                pass


def extract_audio_attachments(attachments: List[Tuple[str, bytes, str]]) -> Tuple[List[OutputFile], str]:
    """Extract the audio of each attached video to MP3. Mirrors compress_attachments."""
    videos = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_video(fn, ct)]
    if not videos:
        return [], "No video to extract audio from — attach a video file first."
    outputs: List[OutputFile] = []
    notes: List[str] = []
    for filename, data, content_type in videos:
        stem = Path(filename).stem or "audio"
        try:
            audio = extract_audio(data, filename, "mp3")
            outputs.append({"filename": f"{stem}.mp3", "data": audio, "content_type": "audio/mpeg"})
            notes.append(f"🎵 {filename} → {_human_size(len(audio))}")
        except Exception as e:
            logger.error(f"extract_audio failed for {filename}: {e}", exc_info=True)
            notes.append(f"❌ {filename}: {e}")
    summary = "## 🎵 Extract audio\n\n" + "\n".join(notes) if notes else "No audio extracted."
    return outputs, summary


def circle_crop(data: bytes, max_dimension: int = IMAGE_MAX_DIMENSION) -> bytes:
    """Center-crop an image to a square and mask it to a circle → a transparent-corner PNG.
    The mask is supersampled (×4) then downscaled for smooth, anti-aliased edges."""
    from PIL import Image, ImageOps, ImageDraw
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGBA")
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        side = min(img.size)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))

        big = side * 2   # 2× supersample is enough for smooth edges (4× allocated up to ~64MP for a 2048px image)
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
        mask = mask.resize((side, side), Image.LANCZOS)

        out_img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        out_img.paste(img, (0, 0), mask)
        out = io.BytesIO()
        out_img.save(out, format="PNG", optimize=True)
        return out.getvalue()


def circle_crop_attachments(attachments: List[Tuple[str, bytes, str]]) -> Tuple[List[OutputFile], str]:
    """Circle-crop each attached image to a transparent PNG. Mirrors compress_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image to circle-crop — attach an image first."
    outputs: List[OutputFile] = []
    notes: List[str] = []
    for filename, data, content_type in images:
        stem = Path(filename).stem or "image"
        try:
            cropped = circle_crop(data)
            outputs.append({"filename": f"{stem}_circle.png", "data": cropped, "content_type": "image/png"})
            notes.append(f"⭕ {filename} → {_human_size(len(cropped))}")
        except Exception as e:
            logger.error(f"circle_crop failed for {filename}: {e}", exc_info=True)
            notes.append(f"❌ {filename}: {e}")
    summary = "## ⭕ Circle crop\n\n" + "\n".join(notes) if notes else "No images cropped."
    return outputs, summary


# ---------------------------------------------------------------------------
# Still image + audio → MP4 (the "narrate"-style image-over-a-song video)
# ---------------------------------------------------------------------------

def _parallax_audio_to_video(image_data: bytes, audio_path: str,
                             duration: Optional[float] = None) -> bytes:
    """Make the photo move (3D parallax) and loop it under the audio track.

    Renders a short seamless parallax loop of the image, then loops that silent clip
    (`-stream_loop -1`, video stream-copied) under the song, cut to the audio (or
    `duration`). Used by image_audio_to_video so the ~40 'sound' gags get a moving
    photo + their music instead of a freeze-frame."""
    from app.services import parallax_service
    loop_mp4 = parallax_service.add_parallax(image_data, amplitude=0.008, zoom=1.02)
    ffmpeg = resolve_ffmpeg()
    tmp_dir = tempfile.mkdtemp(prefix="media_paudio_")
    loop_path = os.path.join(tmp_dir, "loop.mp4")
    out_path = os.path.join(tmp_dir, "out.mp4")
    try:
        with open(loop_path, "wb") as f:
            f.write(loop_mp4)
        cmd = [ffmpeg, "-stream_loop", "-1", "-i", loop_path, "-i", audio_path,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE]
        if duration and duration > 0:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-shortest", "-movflags", "+faststart", "-y", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            with open(out_path, "rb") as f:
                return f.read()
        raise RuntimeError((result.stderr or "")[-300:])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def image_audio_to_video(image_data: bytes, source_filename: str, audio_path: str,
                         duration: Optional[float] = None) -> bytes:
    """Loop an image for the length of an audio track and mux to one MP4.

    The image is made to MOVE first (3D parallax) so the 'sound' gags aren't a
    freeze-frame; on any failure (e.g. depth model missing) it falls back to the
    original still `-loop 1` path. `-shortest` ends the video with the song, even
    dimensions + yuv420p for broad playback, same HW-accel encoder autodetect as
    compress/clip. `duration` (seconds), if given, caps the output. Returns MP4
    bytes; raises RuntimeError if ffmpeg is missing or every encoder fails.
    """
    global _video_encoder_cache
    # Multiple images → slideshow them in order over the one audio track. (Callers that
    # attach several images pass a list of (filename, bytes); a single bytes is the
    # normal one-image path below.)
    if isinstance(image_data, list):
        return images_audio_to_video(image_data, audio_path, duration)

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    if not (audio_path and os.path.exists(audio_path)):
        raise RuntimeError(f"audio track not found: {audio_path}")

    # Preferred: a moving (parallax) photo under the audio. Fall back to a still loop.
    try:
        from app.services import parallax_service
        if parallax_service._session() is not None:
            return _parallax_audio_to_video(image_data, audio_path, duration)
    except Exception as e:
        logger.warning(f"parallax audio path failed ({e}); using still loop")

    tmp_dir = tempfile.mkdtemp(prefix="media_hava_")
    in_suffix = _ext(source_filename) or ".jpg"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(image_data)
        in_path = loopable_still(in_path)     # AVIF/HEIC can't be `-loop 1`'d — see loopable_still

        # Even dimensions are required by yuv420p/H.264 (odd → encoder error).
        scale_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre, vf = [], scale_filter
            if encoder == "h264_nvenc":
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            elif encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                vf = scale_filter + ",format=nv12,hwupload"
                venc = ["-c:v", "h264_vaapi"]
            else:  # libx264
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-tune", "stillimage", "-crf", str(VIDEO_CRF)]
            cmd = (
                [ffmpeg] + pre
                + ["-loop", "1", "-framerate", "2", "-i", in_path, "-i", audio_path,
                   "-vf", vf]
                + venc
                + ["-pix_fmt", "yuv420p", "-r", "12",
                   "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE]
                + (["-t", f"{duration:.3f}"] if duration and duration > 0 else [])
                + ["-shortest", "-movflags", "+faststart", "-y", out_path]
            )
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                if encoder != "libx264":
                    logger.info(f"hava video encoded with GPU encoder: {encoder}")
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"hava encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"image→video failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def images_audio_to_video(images: List[Tuple[str, bytes]], audio_path: str,
                          duration: Optional[float] = None) -> bytes:
    """Slideshow: show each image IN ORDER for an equal slice of the clip, all set to
    one audio track → a single MP4. Used when several images are attached to an audio
    "movie" effect (whoabuddy/prayer/sopranos/…) so all of them make the finished video.

    Images come from different uploads (varying sizes), so each is letterboxed onto a
    common canvas (the first image's even dimensions, capped to 1280 long edge) before
    the concat demuxer joins them — mismatched sizes would otherwise fail concat. Each
    image holds for `duration`/N seconds; `-shortest` ends the video with the audio.
    Reuses the HW-accel encoder autodetect. Returns MP4 bytes.
    """
    global _video_encoder_cache
    from io import BytesIO
    from PIL import Image as _Img, ImageOps as _ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    # audio_path is optional: None → a SILENT slideshow (used by glow's multi-image path,
    # which adds its own colour/sweep over the frames afterwards).
    if audio_path and not os.path.exists(audio_path):
        raise RuntimeError(f"audio track not found: {audio_path}")
    imgs = [(fn, d) for fn, d in (images or []) if d]
    if not imgs:
        raise RuntimeError("no images supplied")
    # A single image has no slideshow to make — use the normal (parallax) path (only when
    # there's audio; a silent single image just falls through to the static-hold render).
    if len(imgs) == 1 and audio_path:
        return image_audio_to_video(imgs[0][1], imgs[0][0], audio_path, duration)

    # Multi-image WITH audio: parallax EACH image (so audio "movie" effects come alive with
    # several images, matching the single-image path) and mux the audio. Each image's parallax
    # orbit is sized to its time slice so the whole thing runs the length of the audio. Falls
    # back to the static-hold slideshow below if the depth model is missing or parallax fails.
    if audio_path:
        try:
            from app.services import parallax_service
            if parallax_service._session() is not None:
                _adur = _probe_duration(audio_path)
                if not _adur or _adur <= 0:
                    _adur = float(duration) if (duration and duration > 0) else 12.0
                _fps = 24
                _fpi = max(24, int(round((_adur / len(imgs)) * _fps)))  # frames per image
                _silent = parallax_service.add_parallax_slideshow(
                    [d for _fn, d in imgs], amplitude=0.008, zoom=1.02, frames=_fpi, fps=_fps)
                _ptmp = tempfile.mkdtemp(prefix="media_paudio_ss_")
                try:
                    _vp = os.path.join(_ptmp, "v.mp4")
                    _op = os.path.join(_ptmp, "out.mp4")
                    with open(_vp, "wb") as _f:
                        _f.write(_silent)
                    _cmd = [ffmpeg, "-i", _vp, "-i", audio_path,
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                            "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE,
                            "-shortest", "-movflags", "+faststart", "-y", _op]
                    _r = subprocess.run(_cmd, capture_output=True, timeout=1800, text=True)
                    if _r.returncode == 0 and os.path.exists(_op) and os.path.getsize(_op) > 0:
                        with open(_op, "rb") as _f:
                            return _f.read()
                    logger.warning(f"parallax-slideshow audio mux failed, using static slideshow: {(_r.stderr or '')[-200:]}")
                finally:
                    shutil.rmtree(_ptmp, ignore_errors=True)
        except Exception as _e:
            logger.warning(f"parallax slideshow unavailable ({_e}); using static slideshow")

    # Canvas = first image's dimensions, capped to a 1280 long edge, rounded even.
    with _Img.open(BytesIO(imgs[0][1])) as _im0:
        _im0 = _ImageOps.exif_transpose(_im0)
        cw, ch = _im0.size
    cap = 1280
    if max(cw, ch) > cap:
        if cw >= ch:
            cw, ch = cap, max(2, round(ch * cap / cw))
        else:
            cw, ch = max(2, round(cw * cap / ch)), cap
    cw = max(2, (cw // 2) * 2)
    ch = max(2, (ch // 2) * 2)

    tmp_dir = tempfile.mkdtemp(prefix="media_slideshow_")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        # Letterbox each image onto the canvas (contain + black pad) so concat sees a
        # uniform size; write sequential PNGs.
        frame_paths = []
        for i, (_fn, d) in enumerate(imgs):
            with _Img.open(BytesIO(d)) as im:
                im = _ImageOps.exif_transpose(im)
                if im.mode != "RGB":
                    im = im.convert("RGB")
                canvas = _Img.new("RGB", (cw, ch), (0, 0, 0))
                fitted = _ImageOps.contain(im, (cw, ch), _Img.LANCZOS)
                canvas.paste(fitted, ((cw - fitted.width) // 2, (ch - fitted.height) // 2))
                fp = os.path.join(tmp_dir, f"frame_{i:04d}.png")
                canvas.save(fp, "PNG")
                frame_paths.append(fp)

        total = float(duration) if (duration and duration > 0) else 12.0
        n = len(frame_paths)
        seg = max(0.6, total / n)
        # Per-image inputs (each held for `seg`s via -loop 1 -t) joined by the concat
        # FILTER. This gives exact, reliable per-image timing — the concat *demuxer*'s
        # image-duration handling was unreliable here (a fixed `-t` truncated the whole
        # thing to one segment), the bug behind "only 1 image".
        img_inputs = []
        for fp in frame_paths:
            img_inputs += ["-loop", "1", "-framerate", "25", "-t", f"{seg:.3f}", "-i", fp]

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre = []
            _concat = ("".join(f"[{i}:v]setsar=1[v{i}];" for i in range(n))
                       + "".join(f"[v{i}]" for i in range(n))
                       + f"concat=n={n}:v=1:a=0[vc]")
            if encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                fc = _concat + ";[vc]format=nv12,hwupload[vout]"
                venc = ["-c:v", "h264_vaapi"]
            elif encoder == "h264_nvenc":
                fc = _concat + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            else:
                fc = _concat + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = (
                [ffmpeg] + pre + img_inputs
                + (["-i", audio_path] if audio_path else [])
                + ["-filter_complex", fc, "-map", "[vout]"]
                + ([f"-map", f"{n}:a", "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE, "-shortest"]
                   if audio_path else ["-an"])
                + venc + ["-pix_fmt", "yuv420p", "-r", "25", "-movflags", "+faststart", "-y", out_path]
            )
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"slideshow encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"slideshow video failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _concat_video_clips(videos: List[bytes]) -> bytes:
    """Concatenate same-size silent MP4 clips IN ORDER via the concat filter (re-encode).
    Reuses the HW-accel encoder autodetect. Returns MP4 bytes."""
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    vids = [v for v in (videos or []) if v]
    if not vids:
        raise RuntimeError("no clips to concatenate")
    if len(vids) == 1:
        return vids[0]
    tmp_dir = tempfile.mkdtemp(prefix="media_concat_")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        inputs, n = [], len(vids)
        for i, v in enumerate(vids):
            p = os.path.join(tmp_dir, f"clip_{i:04d}.mp4")
            with open(p, "wb") as f:
                f.write(v)
            inputs += ["-i", p]
        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]
        last_err = ""
        for encoder in candidates:
            pre = []
            _concat = ("".join(f"[{i}:v]setsar=1[v{i}];" for i in range(n))
                       + "".join(f"[v{i}]" for i in range(n))
                       + f"concat=n={n}:v=1:a=0[vc]")
            if encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                fc = _concat + ";[vc]format=nv12,hwupload[vout]"
                venc = ["-c:v", "h264_vaapi"]
            elif encoder == "h264_nvenc":
                fc = _concat + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            else:
                fc = _concat + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = ([ffmpeg] + pre + inputs + ["-filter_complex", fc, "-map", "[vout]", "-an"]
                   + venc + ["-pix_fmt", "yuv420p", "-r", "25", "-movflags", "+faststart", "-y", out_path])
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"concat encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)
        raise RuntimeError(f"concat clips failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def overlay_corner_character(video_data: bytes, source_filename: str, char_path: str,
                             height_frac: float = 0.34, margin_frac: float = 0.025) -> bytes:
    """Composite a transparent character (static PNG or animated .mov/.gif with alpha) into the
    BOTTOM-RIGHT of a video, scaled to `height_frac` of the video height. Keeps the video's audio.
    Best-effort: returns the original bytes on any failure."""
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available() or not video_data or not char_path or not os.path.exists(char_path):
        return video_data
    tmp = tempfile.mkdtemp(prefix="media_char_")
    try:
        vin = os.path.join(tmp, "in.mp4")
        with open(vin, "wb") as f:
            f.write(video_data)
        W = _probe_width(vin); H = _probe_height(vin)
        if not (W and H):
            return video_data
        ch = max(2, int(H * height_frac))
        mw = int(W * margin_frac); mh = int(H * margin_frac)
        low = char_path.lower()
        if low.endswith(".gif"):
            char_loop = ["-ignore_loop", "0"]
        elif low.endswith((".mov", ".webm", ".mp4")):
            char_loop = ["-stream_loop", "-1"]
        else:                       # static image
            char_loop = ["-loop", "1"]
        base_fc = (f"[1:v]scale=-1:{ch}:flags=lanczos[ov];"
                   f"[0:v][ov]overlay=W-w-{mw}:H-h-{mh}:shortest=1[vc]")
        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]
        out_path = os.path.join(tmp, "out.mp4")
        last_err = ""
        for encoder in candidates:
            pre = []
            if encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                fc = base_fc + ";[vc]format=nv12,hwupload[vout]"
                venc = ["-c:v", "h264_vaapi"]
            elif encoder == "h264_nvenc":
                fc = base_fc + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            else:
                fc = base_fc + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = ([ffmpeg, "-y", "-i", vin] + char_loop + ["-i", char_path,
                    "-filter_complex", fc, "-map", "[vout]", "-map", "0:a?"] + venc
                   + ["-c:a", "copy", "-movflags", "+faststart", out_path])
            r = subprocess.run(cmd, capture_output=True, timeout=600, text=True)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (r.stderr or "")[-300:]
            if os.path.exists(out_path):
                os.unlink(out_path)
        logger.warning(f"overlay_corner_character failed (tried {candidates}); original: {last_err}")
        return video_data
    except Exception as e:
        logger.warning(f"overlay_corner_character error ({e}); returning original")
        return video_data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- TikTok-style branding outro (appended to effect videos) -----------------
_REPO_ROOT_MS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _pick_outro_logo() -> str:
    """The mascot for the end-card and the music-video background. Both composite it onto a GRADIENT
    with `img.paste(lg, pos, lg)` — the image is its own mask — so the asset must actually be
    transparent.

    `icon-512.png` is the PWA icon: fully opaque, with its own (26, 26, 46) navy baked in. Pasted on
    the (20, 22, 38) card that mask does nothing, so it landed as a flat 512x512 rectangle of a
    slightly different purple — visible as a box around the mascot, standing out exactly where the
    branding is supposed to blend. `favicon.png` is the same mascot, larger (512x896, full body) and
    genuinely transparent. Falls back to the opaque icon only if that file is missing."""
    for name in ("favicon.png", "icon-512.png"):
        p = os.path.join(_REPO_ROOT_MS, "static", name)
        if os.path.exists(p):
            return p
    return os.path.join(_REPO_ROOT_MS, "static", "icon-512.png")


_OUTRO_LOGO = _pick_outro_logo()
_OUTRO_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]


def _outro_font(sz: int):
    from PIL import ImageFont
    for p in _OUTRO_FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    try:
        return ImageFont.load_default(sz)
    except TypeError:
        return ImageFont.load_default()


def render_outro_card(W: int, H: int, username: Optional[str] = None, avatar=None):
    """The branded end-card image (PosterChan mascot + 'made with PosterChanAI', plus the user's
    avatar + @username when known). `avatar` is an optional PIL Image."""
    from PIL import Image, ImageDraw, ImageOps
    W = max(2, int(W)); H = max(2, int(H))
    bg = (20, 22, 38)  # match the mascot's navy so the logo blends seamlessly
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    for y in range(H):  # subtle vertical gradient
        f = y / H
        d.line([(0, y), (W, y)], fill=(int(bg[0] + 10 * f), int(bg[1] + 8 * f), int(bg[2] + 18 * f)))
    cx = W // 2

    def _fit(t, target, frac=0.90, floor=8):
        """Largest font up to `target` px whose rendered width fits `frac` of the card width.
        Every text line goes through this so nothing runs off the edges on narrow/tall
        (vertical phone) cards — where height-derived font sizes would otherwise overflow."""
        sz = int(target); mw = int(W * frac)
        while sz > floor:
            bb = d.textbbox((0, 0), t, font=_outro_font(sz))
            if (bb[2] - bb[0]) <= mw:
                break
            sz -= 2
        return _outro_font(sz)

    def _ctext(yy, t, font, fill):
        bb = d.textbbox((0, 0), t, font=font)
        d.text((cx - (bb[2] - bb[0]) / 2, yy), t, font=font, fill=fill)
        return (bb[3] - bb[1])

    y = int(H * 0.09)
    if username:
        av_r = int(min(W, H) * 0.11)
        if avatar is not None:
            try:
                a = ImageOps.fit(avatar.convert("RGB"), (av_r * 2, av_r * 2))
                m = Image.new("L", (av_r * 2, av_r * 2), 0)
                ImageDraw.Draw(m).ellipse([0, 0, av_r * 2, av_r * 2], fill=255)
                img.paste(a, (cx - av_r, y), m)
            except Exception:
                avatar = None
        if avatar is None:
            d.ellipse([cx - av_r, y, cx + av_r, y + av_r * 2], fill=(70, 60, 100))
            _ctext(y + int(av_r * 0.5), username[:1].upper(), _outro_font(int(av_r * 1.0)), (235, 235, 250))
        d.ellipse([cx - av_r, y, cx + av_r, y + av_r * 2], outline=(255, 170, 60), width=max(3, W // 180))
        y += av_r * 2 + int(H * 0.02)
        y += _ctext(y, f"@{username}", _fit(f"@{username}", int(H * 0.05)), (245, 245, 255)) + int(H * 0.03)

    if os.path.exists(_OUTRO_LOGO):
        try:
            from PIL import Image as _I
            lg = _I.open(_OUTRO_LOGO).convert("RGBA")
            lh = int(H * (0.40 if username else 0.50)); lw = int(lg.width * lh / lg.height)
            # Cap the logo to the card width too (tall-narrow cards) — scale by height first,
            # then shrink to fit width if needed, preserving aspect.
            if lw > int(W * 0.6):
                lw = int(W * 0.6); lh = int(lg.height * lw / lg.width)
            lg = lg.resize((max(1, lw), max(1, lh)))
            img.paste(lg, (cx - lw // 2, y), lg)
            y += lh + int(H * 0.015)
        except Exception:
            pass
    _ctext(y, "made with", _fit("made with", int(H * 0.036)), (170, 175, 200)); y += int(H * 0.048)
    _ctext(y, "PosterChan AI", _fit("PosterChan AI", int(H * 0.068)), (255, 170, 60))
    return img


def _probe_has_audio(path: str) -> bool:
    try:
        r = subprocess.run([resolve_ffmpeg().replace("ffmpeg", "ffprobe"), "-v", "error",
                            "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def append_outro(video_data: bytes, source_filename: str = "video.mp4",
                 username: Optional[str] = None, avatar_bytes: Optional[bytes] = None,
                 duration: float = 2.6) -> bytes:
    """Append a ~2.6s branded end-card (TikTok-style) to an effect video. Per-user avatar/@username
    when provided, else a static 'made with PosterChanAI' card. Returns the original bytes unchanged
    on any failure (so branding never breaks an effect)."""
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available() or not video_data:
        return video_data
    tmp = tempfile.mkdtemp(prefix="media_outro_")
    try:
        vin = os.path.join(tmp, "in.mp4")
        with open(vin, "wb") as f:
            f.write(video_data)
        W = _probe_width(vin); H = _probe_height(vin)
        if not (W and H):
            return video_data
        W -= W % 2; H -= H % 2
        has_audio = _probe_has_audio(vin)
        vdur = _probe_duration(vin) or 6.0
        from PIL import Image as _Image
        avatar = None
        if avatar_bytes:
            try:
                avatar = _Image.open(io.BytesIO(avatar_bytes))
            except Exception:
                avatar = None
        card = render_outro_card(W, H, username, avatar)
        cpath = os.path.join(tmp, "card.png"); card.save(cpath)
        out_path = os.path.join(tmp, "out.mp4")
        sr = 44100

        inputs = ["-i", vin, "-loop", "1", "-t", f"{duration:.3f}", "-i", cpath,
                  "-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"anullsrc=r={sr}:cl=stereo"]
        if has_audio:
            a0src = "[0:a]"
        else:
            inputs += ["-f", "lavfi", "-t", f"{max(vdur, 0.1):.3f}", "-i", f"anullsrc=r={sr}:cl=stereo"]
            a0src = "[3:a]"
        vf = f"scale={W}:{H}:force_original_aspect_ratio=disable,setsar=1,fps=30"
        base_fc = (
            f"[0:v]{vf}[v0];"
            f"[1:v]{vf},fade=t=in:st=0:d=0.4[v1];"
            f"{a0src}aresample={sr},aformat=channel_layouts=stereo[a0];"
            f"[2:a]aresample={sr},aformat=channel_layouts=stereo[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][aout]"
        )
        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]
        last_err = ""
        for encoder in candidates:
            pre = []
            if encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                fc = base_fc + ";[vc]format=nv12,hwupload[vout]"
                venc = ["-c:v", "h264_vaapi"]
            elif encoder == "h264_nvenc":
                fc = base_fc + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            else:
                fc = base_fc + ";[vc]format=yuv420p[vout]"
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = ([ffmpeg, "-y"] + pre + inputs + ["-filter_complex", fc,
                    "-map", "[vout]", "-map", "[aout]"] + venc
                   + ["-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE, "-movflags", "+faststart", out_path])
            r = subprocess.run(cmd, capture_output=True, timeout=600, text=True)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (r.stderr or "")[-300:]
            if os.path.exists(out_path):
                os.unlink(out_path)
        logger.warning(f"append_outro failed (tried {candidates}); returning original: {last_err}")
        return video_data
    except Exception as e:
        logger.warning(f"append_outro error ({e}); returning original")
        return video_data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_music_background(W: int, H: int, title: str = ""):
    """A generic branded backdrop for music videos: the PosterChan logo + 'PosterChanAI' on a dark
    gradient, with an optional song-title/prompt line. Gives the song some visuals BEFORE the
    branded end-card outro (the 'watermark'). Distinct from `render_outro_card`."""
    from PIL import Image, ImageDraw
    W = max(2, int(W)); H = max(2, int(H))
    bg = (20, 22, 38)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    for y in range(H):  # subtle vertical gradient
        f = y / H
        d.line([(0, y), (W, y)], fill=(int(bg[0] + 12 * f), int(bg[1] + 6 * f), int(bg[2] + 26 * f)))
    cx = W // 2

    def _fit(t, target, frac=0.9, floor=8):
        sz = int(target); mw = int(W * frac)
        while sz > floor:
            bb = d.textbbox((0, 0), t, font=_outro_font(sz))
            if (bb[2] - bb[0]) <= mw:
                break
            sz -= 2
        return _outro_font(sz)

    def _ctext(yy, t, font, fill):
        bb = d.textbbox((0, 0), t, font=font)
        d.text((cx - (bb[2] - bb[0]) / 2, yy), t, font=font, fill=fill)
        return bb[3] - bb[1]

    y = int(H * 0.16)
    if os.path.exists(_OUTRO_LOGO):
        try:
            lg = Image.open(_OUTRO_LOGO).convert("RGBA")
            lh = int(H * 0.42); lw = int(lg.width * lh / lg.height)
            if lw > int(W * 0.5):
                lw = int(W * 0.5); lh = int(lg.height * lw / lg.width)
            lg = lg.resize((max(1, lw), max(1, lh)))
            img.paste(lg, (cx - lw // 2, y), lg)
            y += lh + int(H * 0.03)
        except Exception:
            pass
    _ctext(y, "PosterChan AI", _fit("PosterChan AI", int(H * 0.10)), (255, 170, 60))
    y += int(H * 0.12)
    if title:
        t = title if len(title) <= 60 else title[:57] + "..."
        _ctext(y, t, _fit(t, int(H * 0.05)), (210, 212, 230))
    return img


def make_music_video(audio_data: bytes, audio_ext: str = "mp3", title: str = "",
                     W: int = 1280, H: int = 720, add_outro: bool = True) -> bytes:
    """Wrap a generated song in a branded video: a generic PosterChan background shown for the
    song's full duration with the song as the audio track, then the branded end-card outro (the
    'watermark') appended. Returns mp4 bytes, or b'' on failure (caller falls back to raw audio)."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available() or not audio_data:
        return b""
    tmp = tempfile.mkdtemp(prefix="media_musicvid_")
    try:
        ain = os.path.join(tmp, f"song.{(audio_ext or 'mp3')}")
        with open(ain, "wb") as f:
            f.write(audio_data)
        W -= W % 2; H -= H % 2
        bgp = os.path.join(tmp, "bg.png")
        render_music_background(W, H, title).save(bgp)
        out = os.path.join(tmp, "out.mp4")
        # Still image looped for the song's length (-shortest stops at audio end). libx264
        # -tune stillimage is ideal/cheap here — no HW encoder needed for one static frame.
        cmd = [ffmpeg, "-y", "-loop", "1", "-i", bgp, "-i", ain,
               "-c:v", "libx264", "-tune", "stillimage", "-preset", VIDEO_PRESET,
               "-crf", str(VIDEO_CRF), "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, timeout=900, text=True)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            logger.warning(f"make_music_video failed: {(r.stderr or '')[-300:]}")
            return b""
        with open(out, "rb") as f:
            vid = f.read()
        if add_outro:
            vid = append_outro(vid, "music.mp4")
        return vid
    except Exception as e:
        logger.warning(f"make_music_video error ({e})")
        return b""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_generated_video(frames, fps: int = 16, add_outro: bool = True, title: str = "",
                         upscale_height: int = 0) -> bytes:
    """Assemble generated video frames (list of HxWx3 uint8 numpy arrays / PIL images) into a branded
    MP4: encode the frames (optionally lanczos-upscaling to `upscale_height` for 720p/1080p output),
    then append the same end-card "watermark" outro music uses. Returns mp4 bytes, or b'' on failure
    (caller falls back). Mirrors make_music_video's branding flow."""
    import numpy as _np
    from PIL import Image as _Image
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available() or not frames:
        return b""
    tmp = tempfile.mkdtemp(prefix="media_genvid_")
    try:
        src_h = None
        for i, fr in enumerate(frames):
            img = fr if isinstance(fr, _Image.Image) else _Image.fromarray(_np.asarray(fr).astype("uint8"))
            if img.mode != "RGB":
                img = img.convert("RGB")
            # pad to even dims (libx264/yuv420p requires it)
            w, h = img.size
            if w % 2 or h % 2:
                img = img.crop((0, 0, w - (w % 2), h - (h % 2)))
            src_h = img.size[1]
            img.save(os.path.join(tmp, f"f{i:05d}.png"))
        out = os.path.join(tmp, "out.mp4")
        # Upscale the finished clip to the requested output height (the 1.3B model only renders ~480p
        # natively). -2 keeps aspect + even width. Only UP, never down.
        vf = []
        if upscale_height and src_h and int(upscale_height) > src_h:
            vf = ["-vf", f"scale=-2:{int(upscale_height)}:flags=lanczos"]
        cmd = [ffmpeg, "-y", "-framerate", str(max(1, int(fps))),
               "-i", os.path.join(tmp, "f%05d.png")] + vf + [
               "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, timeout=600, text=True)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            logger.warning(f"make_generated_video encode failed: {(r.stderr or '')[-300:]}")
            return b""
        with open(out, "rb") as f:
            vid = f.read()
        if add_outro:
            vid = append_outro(vid, "video.mp4")
        return vid
    except Exception as e:
        logger.warning(f"make_generated_video error ({e})")
        return b""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def images_glow_video(images: List[Tuple[str, bytes]], per_image: float = 4.0) -> bytes:
    """Several images → ONE glowing clip: each image gets the FULL glow (breathing zoom +
    colour pop + light sweep) for `per_image`s, played IN ORDER. Each image is letterboxed
    onto a common canvas (first image's even dims, capped 1280) so the per-image glow clips
    concat cleanly. (Glowing each image individually — vs one sweep across a slideshow —
    so every image actually glows.) Returns MP4 bytes."""
    from io import BytesIO
    from PIL import Image as _Img, ImageOps as _ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass
    imgs = [(fn, d) for fn, d in (images or []) if d]
    if not imgs:
        raise RuntimeError("no images supplied")
    if len(imgs) == 1:
        return image_glow_video(imgs[0][1], imgs[0][0], duration=per_image)

    with _Img.open(BytesIO(imgs[0][1])) as _im0:
        _im0 = _ImageOps.exif_transpose(_im0)
        cw, ch = _im0.size
    cap = 1280
    if max(cw, ch) > cap:
        if cw >= ch:
            cw, ch = cap, max(2, round(ch * cap / cw))
        else:
            cw, ch = max(2, round(cw * cap / ch)), cap
    cw = max(2, (cw // 2) * 2)
    ch = max(2, (ch // 2) * 2)

    clips = []
    for _fn, d in imgs:
        with _Img.open(BytesIO(d)) as im:
            im = _ImageOps.exif_transpose(im)
            if im.mode != "RGB":
                im = im.convert("RGB")
            canvas = _Img.new("RGB", (cw, ch), (0, 0, 0))
            fitted = _ImageOps.contain(im, (cw, ch), _Img.LANCZOS)
            canvas.paste(fitted, ((cw - fitted.width) // 2, (ch - fitted.height) // 2))
            buf = BytesIO()
            canvas.save(buf, "PNG")
        clips.append(image_glow_video(buf.getvalue(), "frame.png", duration=per_image))
    return _concat_video_clips(clips)


def frames_to_video(frames, fps: int = 14, loops: int = 1) -> bytes:
    """Encode a sequence of RGB PIL frames into a silent H.264 MP4.

    `frames` is one pass of full-size RGB ``PIL.Image`` frames; `loops` repeats the
    whole pass that many times on disk (so a periodic motion encoded as one wrapping
    cycle plays back seamlessly without relying on the player to loop the file).
    Reuses the same HW-accel encoder autodetect (NVENC → VAAPI → libx264) as
    compress/clip; no audio track (`-an`). Returns MP4 bytes; raises RuntimeError if
    ffmpeg is missing, no frames are given, or every encoder fails.

    `frames` may be a GENERATOR, and with the default ``loops=1`` it is consumed lazily —
    each frame is written to disk and released. That matters for the long clips (`talk`
    renders one full-size frame per audio frame, so 30s is 600 of them: materialising the
    list first is hundreds of MB for no reason). `loops > 1` has to replay the pass, so it
    still materialises.

    Used by the animated "effect" gags (fire) — the effect content is re-rendered per
    frame so the flames flicker, rather than the flat still that the camera-motion
    modifiers (zoom/shake) move.
    """
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    loops = max(int(loops), 1)
    frames = frames or []
    if loops > 1:
        frames = list(frames)
        if not frames:
            raise RuntimeError("no frames to encode")

    tmp_dir = tempfile.mkdtemp(prefix="media_frames_")
    pattern = os.path.join(tmp_dir, "f_%05d.png")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        idx = 0
        for _ in range(loops):
            for fr in frames:
                # compress_level=1: these PNGs live for one ffmpeg run and are deleted. At
                # Pillow's default level the ENCODE dominates everything else — measured on
                # a 960x665 frame: 167ms to write vs 1ms to render it, so a 400-frame clip
                # spent 67s of its 68s zipping temp files. Level 1 is 38ms for 10% more
                # bytes on disk, and the video is identical (PNG is lossless either way).
                fr.save(pattern % idx, format="PNG", compress_level=1)
                idx += 1
        if idx == 0:
            raise RuntimeError("no frames to encode")

        # Even dimensions are required by yuv420p/H.264 (odd → encoder error).
        scale_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre, vf = [], scale_filter
            if encoder == "h264_nvenc":
                venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(VIDEO_CRF)]
            elif encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                vf = scale_filter + ",format=nv12,hwupload"
                venc = ["-c:v", "h264_vaapi", "-qp", str(VIDEO_CRF)]
            else:  # libx264
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = (
                [ffmpeg, "-framerate", str(fps), "-i", pattern, "-vf", vf]
                + venc
                + ["-pix_fmt", "yuv420p", "-r", str(fps),
                   "-an", "-movflags", "+faststart", "-y", out_path]
            )
            if pre:
                cmd = [ffmpeg] + pre + cmd[1:]
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                if encoder != "libx264":
                    logger.info(f"frames→video encoded with GPU encoder: {encoder}")
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"frames→video encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"frames→video failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def mux_audio_loop(video_data: bytes, audio_path: str,
                   source_filename: str = "video.mp4") -> bytes:
    """Mux a (looped) audio track under a silent video, cut to the VIDEO's length.

    The audio is stream-looped (`-stream_loop -1`) so a short clip fills a longer
    silent clip, and `-shortest` ends the muxed file at the video's length (the video
    is the authoritative/shorter stream here — the opposite of `image_audio_to_video`,
    which cuts to the audio). Video is stream-copied (no re-encode); audio → AAC.
    Best-effort: returns the original silent bytes unchanged on any failure — so a
    missing/broken audio asset never breaks the effect."""
    ffmpeg = resolve_ffmpeg()
    if not (ffmpeg_available() and audio_path and os.path.exists(audio_path)):
        return video_data
    tmp_dir = tempfile.mkdtemp(prefix="media_muxaudio_")
    in_path = os.path.join(tmp_dir, "in.mp4")
    out_path = os.path.join(tmp_dir, "out.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(video_data)
        cmd = [ffmpeg, "-i", in_path, "-stream_loop", "-1", "-i", audio_path,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE,
               "-shortest", "-movflags", "+faststart", "-y", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            with open(out_path, "rb") as f:
                return f.read()
        logger.warning(f"mux_audio_loop failed, returning silent video: {(result.stderr or '')[-300:]}")
        return video_data
    except Exception as e:
        logger.warning(f"mux_audio_loop error ({e}), returning silent video")
        return video_data
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---- ALPHA (transparent-background) effect layers for the Meme Builder ---------------------------
# These encode an effect onto a FULLY TRANSPARENT canvas so it can be dropped onto the Meme Builder
# timeline as a compositable layer over whatever is beneath it (see meme_builder_service /
# client.py:/meme/effect). They deliberately do NOT append the outro/branding card — branding belongs
# on the FINAL exported meme, never on a sub-layer that gets composited into it.
#
# Codec choice — VP9-alpha WebM (yuva420p), and SILENT:
#   The layer has to be playable in the BROWSER's <video> preview (the Meme Builder shows each video
#   layer live), and browsers cannot decode ProRes .mov — so a ProRes layer rendered invisibly in the
#   editor even though it composited fine server-side ("nakedman didn't display"). VP9-alpha WebM plays
#   transparently in every Chromium/Firefox <video> AND composites in ffmpeg — the earlier "WebM loses
#   alpha" result was a DECODER bug, not a container one: ffmpeg's native `vp9` decoder ignores the alpha
#   layer, so the composite must decode the input with `-c:v libvpx-vp9` (meme_builder_service does this
#   for .webm layers). It is also ~1000x smaller than ProRes (KB not MB).
#   SILENT: adding an audio stream to a VP9-alpha WebM corrupts the alpha (all-transparent) on this
#   ffmpeg, whether muxed by copy OR re-encoded in one pass — verified. So the effect's SOUND is NOT in
#   the clip; it rides the meme layer's existing per-layer `sound` field instead (client sets it to the
#   effect name, which is already in the sound catalogue). `-cpu-used 5 -deadline good` keeps the encode
#   quick enough for an on-demand layer.
_ALPHA_VCODEC = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "34",
                 "-deadline", "good", "-cpu-used", "5", "-row-mt", "1"]


def frames_to_alpha_video(frames, fps: int = 20, loops: int = 1) -> bytes:
    """Encode a sequence of RGBA PIL frames into a SILENT VP9-alpha .webm with a real alpha channel.

    `frames` is one pass of full-size RGBA ``PIL.Image`` frames (a wrapping animation cycle); `loops`
    repeats that pass on disk so a periodic motion plays seamlessly. The frames' transparency is
    preserved through the encode (see _ALPHA_VCODEC), so the result both PLAYS transparently in a
    browser <video> and composites over anything beneath it in the Meme Builder (decoded with
    `-c:v libvpx-vp9`). No audio (it would corrupt the alpha) and no outro — this is a sub-layer; its
    sound rides the meme layer's `sound` field.

    Returns .webm bytes; raises RuntimeError if ffmpeg is missing, no frames are given, or the encode
    fails. No encoder ladder — VP9 is the one codec here that is both alpha-capable and browser-playable.
    """
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    frames = list(frames or [])
    if not frames:
        raise RuntimeError("no frames to encode")

    tmp_dir = tempfile.mkdtemp(prefix="media_alpha_")
    pattern = os.path.join(tmp_dir, "f_%05d.png")
    out_path = os.path.join(tmp_dir, "output.webm")
    try:
        idx = 0
        for _ in range(max(int(loops), 1)):
            for fr in frames:
                # RGBA PNG carries the alpha the VP9 encoder reads; force the mode so an accidental
                # RGB frame doesn't silently produce an opaque layer. compress_level=1 for the
                # reason in frames_to_video — the encode of a throwaway temp file was costing far
                # more than rendering the frame.
                fr.convert("RGBA").save(pattern % idx, format="PNG", compress_level=1)
                idx += 1
        cmd = ([ffmpeg, "-framerate", str(fps), "-i", pattern]
               + _ALPHA_VCODEC
               + ["-r", str(fps), "-an", "-y", out_path])
        result = subprocess.run(cmd, capture_output=True, timeout=1800, text=True)
        if not (result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            raise RuntimeError(f"alpha frames→video failed: {(result.stderr or '')[-400:]}")
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def still_to_alpha_video(image, dur: float = 6.0, fps: int = 6) -> bytes:
    """Like frames_to_alpha_video but for a STILL RGBA image held for `dur` seconds (a static character
    overlay). Uses `-loop 1 -t dur` on a single PNG so a motionless layer is not stored as hundreds of
    identical frames. Same SILENT VP9-alpha .webm codec, no branding, no audio.

    fps is deliberately LOW (6): the image never changes, so a higher rate only multiplies identical
    frames (and file size) for no visual gain — the meme renderer's framesync duplicates frames up to the
    project rate on composite. Kept ≥ a few fps so timeline scrubbing/preview stays smooth.
    """
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    dur = max(0.3, min(float(dur or 6.0), 30.0))
    tmp_dir = tempfile.mkdtemp(prefix="media_alphastill_")
    png_path = os.path.join(tmp_dir, "still.png")
    out_path = os.path.join(tmp_dir, "output.webm")
    try:
        image.convert("RGBA").save(png_path, format="PNG")
        cmd = ([ffmpeg, "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", png_path]
               + _ALPHA_VCODEC
               + ["-r", str(fps), "-an", "-y", out_path])
        result = subprocess.run(cmd, capture_output=True, timeout=600, text=True)
        if not (result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            raise RuntimeError(f"alpha still→video failed: {(result.stderr or '')[-400:]}")
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def alpha_clip_to_video(src_path: str, dur: float = None) -> bytes:
    """Re-encode a ready-made TRANSPARENT clip (the ProRes 4444 `.mov` overlays in assets/ — beavis,
    reze, makima, …) into the SAME silent VP9-alpha .webm that frames_to_alpha_video and
    still_to_alpha_video produce, so it drops onto the Meme Builder timeline like any other layer.

    These clips already carry a real alpha channel (yuva444p12le); they only need the codec the
    browser preview and the compositing render both expect. SILENT on purpose — audio would corrupt
    VP9 alpha, and the effect's sound rides the layer's `sound` field instead (see
    meme_builder_service.alpha_effect_catalog).

    `dur` trims; it never pads, because looping a dance mid-step reads as a glitch and the caller
    already sizes the timeline slot from the clip's natural length."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    if not (src_path and os.path.exists(src_path)):
        raise RuntimeError(f"alpha clip not found: {src_path}")
    tmp_dir = tempfile.mkdtemp(prefix="media_alphaclip_")
    out_path = os.path.join(tmp_dir, "output.webm")
    try:
        cmd = [ffmpeg, "-i", src_path]
        if dur:
            cmd += ["-t", f"{max(0.3, min(float(dur), 30.0)):.3f}"]
        cmd += _ALPHA_VCODEC + ["-an", "-y", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=600, text=True)
        if not (result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            raise RuntimeError(f"alpha clip→video failed: {(result.stderr or '')[-400:]}")
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def image_gif_overlay_video(image_data: bytes, source_filename: str, gif_path: str,
                            duration: float = 6.0, audio_path: Optional[str] = None,
                            height_frac: float = 0.55) -> bytes:
    """Composite a looping transparent overlay (animated GIF *or* a video with an
    alpha channel, e.g. a ProRes 4444 .mov / VP8 .webm) onto the lower part of a still
    image and render to one fixed-length MP4.

    The background image is held static (`-loop 1`) while the overlay loops — a GIF via
    `-ignore_loop 0`, any other (video) overlay via `-stream_loop -1`; the clip runs
    exactly `duration` seconds. The overlay is scaled to `height_frac` of the image
    height (aspect kept), centred horizontally and anchored to the bottom; its
    transparency is preserved so only the artwork sits over the photo. If `audio_path`
    is given it's looped (`-stream_loop -1`) to fill the whole clip duration. Reuses the
    same HW-accel encoder autodetect (NVENC → VAAPI → libx264) as the audio effects.
    Returns MP4 bytes; raises RuntimeError if ffmpeg is missing or every encoder fails.
    """
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    if not (gif_path and os.path.exists(gif_path)):
        raise RuntimeError(f"overlay not found: {gif_path}")
    has_audio = bool(audio_path and os.path.exists(audio_path))
    height_frac = min(max(height_frac, 0.1), 1.0)
    # A GIF loops with -ignore_loop 0; an alpha video loops with -stream_loop -1.
    is_gif_overlay = gif_path.lower().endswith(".gif")
    overlay_loop = ["-ignore_loop", "0"] if is_gif_overlay else ["-stream_loop", "-1"]
    # `-stream_loop` restarts the CONTAINER, and the restart lands the first frame of each repeat
    # a couple of frames late — every loop after the first ran ~0.2s behind, which desynced
    # `makima`'s muzzle flashes from her gunshots and dropped some one-frame flashes outright.
    # Re-stamping the decoded frames at the overlay's own rate makes the loop seamless: frame N is
    # simply at N/rate, whatever the container did at the seam.
    overlay_fps = _probe_rate(gif_path) if not is_gif_overlay else 0.0
    retime = f"setpts=N/{overlay_fps:.6f}/TB," if overlay_fps > 0 else ""

    tmp_dir = tempfile.mkdtemp(prefix="media_overlay_")
    in_suffix = _ext(source_filename) or ".jpg"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(image_data)
        in_path = loopable_still(in_path)     # AVIF/HEIC can't be `-loop 1`'d — see loopable_still

        # Make the background photo MOVE (3D parallax) instead of a freeze-frame: build
        # a short parallax loop and stream-loop it as input 0; the alpha overlay (clay /
        # chimp) rides on top. Falls back to the still `-loop 1` image if depth is missing.
        bg_input = ["-loop", "1", "-framerate", "12", "-i", in_path]
        bg_path = in_path
        try:
            from app.services import parallax_service
            if parallax_service._session() is not None:
                _ploop = os.path.join(tmp_dir, "pbg.mp4")
                with open(_ploop, "wb") as f:
                    f.write(parallax_service.add_parallax(image_data, amplitude=0.008, zoom=1.02))
                bg_input = ["-stream_loop", "-1", "-i", _ploop]
                bg_path = _ploop
        except Exception as e:
            logger.warning(f"parallax overlay background failed ({e}); using still")

        # The overlay is sized to `height_frac` of the PHOTO's height, then centred horizontally
        # and anchored to the bottom edge.
        #
        # This used to be `scale2ref=w=-1:h=rh*frac`, which silently DISTORTED every animated
        # overlay: scale2ref resolves `w=-1` against the REFERENCE, not the overlay's own aspect,
        # so the overlay came out with the photo's aspect ratio — a 504x560 dancer rendered
        # 720x360 on a landscape photo (2.2x too wide) and 360x720 on a portrait one. Measuring
        # the photo here and scaling by an absolute height (`-2` = even width, aspect kept) is
        # both correct and independent of scale2ref's argument semantics.
        # Measure the height of what ACTUALLY reaches the filter graph, not of the source photo.
        # parallax_service caps its working long edge at _MAXDIM (1280), so for any photo bigger than
        # that the background arrives downscaled while `height_frac` was still being applied to the
        # ORIGINAL height: a 2900x4096 phone shot became a 906x1280 background carrying an overlay
        # sized 0.45*4096 = 1843px — 144% of the canvas height, so the overlay blew past the frame
        # instead of sitting in it ("the effect didn't adapt to the image size"). Every overlay effect
        # (chimp/clay/reze/vibe/rebecca/makima) had this on any photo with a long edge over 1280.
        _bg_h = _probe_height(bg_path)
        if _bg_h <= 0:
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(io.BytesIO(image_data)) as _probe:
                    _bg_h = int(_probe.size[1])
            except Exception as e:
                logger.warning(f"overlay: could not read image height ({e}); falling back to scale2ref")
        if _bg_h > 0:
            ov_h = int(_bg_h * height_frac)
            # `height_frac` bounds the HEIGHT only, so an overlay wider than it is tall runs off the
            # sides of a narrow frame — a 382x323 pair at 0.45 needs 1277px of width on a 1080px-wide
            # phone shot and lost its outer edges. Bound the width too, so the overlay fits INSIDE the
            # frame whatever its aspect. This can only ever shrink an overlay that was already being
            # cropped, so nothing that fits today changes size.
            _bg_w, _ov_w, _ov_h = _probe_width(bg_path), _probe_width(gif_path), _probe_height(gif_path)
            if _bg_w > 0 and _ov_w > 0 and _ov_h > 0:
                ov_h = min(ov_h, int(_bg_w * _ov_h / _ov_w))
            ov_h = max(2, ov_h // 2 * 2)
            base = (
                # `overlay` emits one frame per BACKGROUND frame, so the background's clock is the
                # output's clock. The parallax loop runs at its own rate and is itself
                # `-stream_loop`ed, which held the overlay ~2 frames behind for the whole render —
                # `makima`'s flashes fired 0.17s after her gunshots. Resampling BOTH branches to
                # 12fps puts them on one clock and the two line up exactly.
                "[0:v]fps=12,scale=trunc(iw/2)*2:trunc(ih/2)*2[bg];"
                f"[1:v]format=rgba,{retime}fps=12,scale=-2:{ov_h}[ov];"
                "[bg][ov]overlay=x=(W-w)/2:y=H-h:shortest=0"
            )
        else:
            base = (
                "[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2[bg0];"
                f"[1:v]format=rgba,{retime}fps=12[g];"
                f"[g][bg0]scale2ref=w=-1:h=rh*{height_frac:.3f}[ov][bg];"
                "[bg][ov]overlay=x=(W-w)/2:y=H-h:shortest=0"
            )

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre = []
            if encoder == "h264_nvenc":
                fc = base + ",format=yuv420p[v]"
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            elif encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                fc = base + ",format=nv12,hwupload[v]"
                venc = ["-c:v", "h264_vaapi"]
            else:  # libx264
                fc = base + ",format=yuv420p[v]"
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = (
                [ffmpeg] + pre
                + bg_input
                + overlay_loop + ["-i", gif_path]
                # -stream_loop -1 repeats the track to fill the whole clip duration.
                + (["-stream_loop", "-1", "-i", audio_path] if has_audio else [])
                + ["-filter_complex", fc, "-map", "[v]"]
                + (["-map", "2:a", "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE] if has_audio else [])
                + venc
                + ["-r", "12", "-t", f"{duration:.3f}",
                   "-movflags", "+faststart", "-y", out_path]
            )
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                if encoder != "libx264":
                    logger.info(f"gif-overlay video encoded with GPU encoder: {encoder}")
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"gif-overlay encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"gif-overlay→video failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _probe_rate(path: str) -> float:
    """Video frame rate in fps (0.0 if unknown), from the stream's r_frame_rate ("12/1")."""
    ffmpeg = resolve_ffmpeg()
    ffprobe = ffmpeg[:-6] + "ffprobe" if ffmpeg.endswith("ffmpeg") else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=60, text=True,
        )
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if "/" in line:
                num, den = line.split("/", 1)
                if float(den):
                    return float(num) / float(den)
            elif line.replace(".", "", 1).isdigit():
                return float(line)
        return 0.0
    except Exception:
        return 0.0


def _probe_dim(path: str, dim: str) -> int:
    """Video width/height in px via ffprobe (0 if unknown). `dim` is 'width'|'height'."""
    ffmpeg = resolve_ffmpeg()
    ffprobe = ffmpeg[:-6] + "ffprobe" if ffmpeg.endswith("ffmpeg") else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             f"stream={dim}", "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=60, text=True,
        )
        # take the first numeric line (some files report extra fields)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return 0
    except Exception:
        return 0


def _probe_height(path: str) -> int:
    """Video height in px via ffprobe (0 if unknown)."""
    return _probe_dim(path, "height")


def _probe_width(path: str) -> int:
    """Video width in px via ffprobe (0 if unknown)."""
    return _probe_dim(path, "width")


def _wrap_caption(text: str, font, max_width: int) -> List[str]:
    """Greedy word-wrap `text` to `max_width` px for `font` (PIL); a single word
    wider than the line is hard-broken so it can never overflow."""
    def w(s: str) -> int:
        return int(font.getlength(s))
    lines: List[str] = []
    for word in text.split():
        if lines and w(lines[-1] + " " + word) <= max_width:
            lines[-1] += " " + word
        elif w(word) <= max_width:
            lines.append(word)
        else:  # hard-break an over-long single word
            piece = ""
            for ch in word:
                if w(piece + ch) <= max_width or not piece:
                    piece += ch
                else:
                    lines.append(piece)
                    piece = ch
            if piece:
                lines.append(piece)
    return lines or [text]


def caption_video(video_data: bytes, text: str, font_path: str = "") -> bytes:
    """Burn an outlined white meme caption across the lower part of a video
    (static overlay — stays put while the video zooms/shakes). Keeps the audio.

    The caption is word-wrapped to the video WIDTH and auto-sized so it never runs
    off the sides — critical for narrow/vertical (mobile) videos where ffmpeg
    drawtext, which does no wrapping of its own, would otherwise clip the text.
    Each wrapped line is drawn as its own centred drawtext. Returns MP4 bytes."""
    from PIL import ImageFont
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    text = (text or "").strip().upper()
    if not text:
        return video_data

    tmp_dir = tempfile.mkdtemp(prefix="media_caption_")
    vin = os.path.join(tmp_dir, "in.mp4")
    out_path = os.path.join(tmp_dir, "out.mp4")
    try:
        with open(vin, "wb") as f:
            f.write(video_data)
        W = _probe_width(vin) or 1280
        H = _probe_height(vin) or 720
        margin = max(10, H // 22)
        margin_x = max(8, int(W * 0.04))
        max_width = W - 2 * margin_x
        max_height = int(H * 0.5)  # caption lives in the lower half

        def _font(sz: int):
            if font_path and os.path.exists(font_path):
                return ImageFont.truetype(font_path, sz)
            try:
                return ImageFont.load_default(sz)
            except TypeError:
                return ImageFont.load_default()

        # Auto-size: largest font (from ~1/8 height down) whose wrapped block fits
        # the width and the lower-half height.
        fs = max(16, H // 11)
        lines = [text]
        for size in range(max(int(H / 8), 16), 11, -2):
            f = _font(size)
            wrapped = _wrap_caption(text, f, max_width)
            line_h = int(size * 1.3)
            if line_h * len(wrapped) <= max_height:
                fs, lines = size, wrapped
                break
        else:
            # Nothing fit the lower half even at the floor — use the smallest size
            # (best effort; very long captions on short clips).
            f = _font(12)
            fs, lines = 12, _wrap_caption(text, f, max_width)

        bw = max(2, fs // 12)
        line_h = int(fs * 1.3)
        total_h = line_h * len(lines)
        y0 = H - margin - total_h  # top of the caption block
        fontopt = f"fontfile='{font_path}':" if font_path and os.path.exists(font_path) else ""

        # One centred drawtext per line (drawtext has no per-line centering); each
        # line goes to its own textfile to avoid filter-escaping issues.
        draws = []
        for i, ln in enumerate(lines):
            tf = os.path.join(tmp_dir, f"cap_{i}.txt")
            with open(tf, "w") as fh:
                fh.write(ln)
            y = y0 + i * line_h
            draws.append(
                f"drawtext={fontopt}textfile='{tf}':fontcolor=white:bordercolor=black:"
                f"borderw={bw}:fontsize={fs}:x=(w-text_w)/2:y={y}"
            )
        vf = ",".join(draws)
        cmd = [
            ffmpeg, "-i", vin, "-vf", vf,
            "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-y", out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
        if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            # Some clips have no audio stream to copy — retry encoding audio.
            cmd[cmd.index("copy")] = "aac"
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode != 0 or not os.path.exists(out_path):
                raise RuntimeError(f"caption_video failed: {(result.stderr or '')[-300:]}")
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _probe_duration(path: str) -> float:
    """Media duration in seconds via ffprobe (0.0 if unknown)."""
    ffmpeg = resolve_ffmpeg()
    ffprobe = ffmpeg[:-6] + "ffprobe" if ffmpeg.endswith("ffmpeg") else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=60, text=True,
        )
        return float((out.stdout or "").strip())
    except Exception:
        return 0.0


def _render_motion_video(image_data: bytes, source_filename: str, vf_builder,
                         duration: float = 4.0, audio_path: Optional[str] = None) -> bytes:
    """Render a motion clip from a still image. `vf_builder(W, H, n_frames)`
    returns the video-filter chain (scale + motion, NO format/hwupload suffix);
    the encoder loop appends the right pixel-format step per encoder. Optional
    `audio_path` is muxed in. Even dimensions + yuv420p for broad playback;
    reuses the HW-accel encoder autodetect (NVENC → VAAPI → libx264).
    Returns MP4 bytes; raises RuntimeError if ffmpeg is missing or all fail.
    """
    global _video_encoder_cache
    from PIL import Image as _PILImage
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    if audio_path and not os.path.exists(audio_path):
        audio_path = None

    tmp_dir = tempfile.mkdtemp(prefix="media_motion_")
    in_suffix = _ext(source_filename) or ".jpg"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(image_data)
        # AVIF/HEIC can't be `-loop 1`'d (see loopable_still) — and converting also gets the size
        # probe below off a format plain Pillow may not open, instead of the 1280x720 fallback.
        in_path = loopable_still(in_path)
        # Target = input dims rounded to even (yuv420p/H.264 require even).
        try:
            with _PILImage.open(in_path) as im:
                W, H = im.size
        except Exception:
            W, H = 1280, 720
        W = max(2, (W // 2) * 2)
        H = max(2, (H // 2) * 2)
        fps = 25
        dur = max(0.5, float(duration))
        n_frames = max(2, int(round(fps * dur)))
        motion = vf_builder(W, H, n_frames)

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre, vf = [], motion + ",format=yuv420p"
            if encoder == "h264_nvenc":
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            elif encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                vf = motion + ",format=nv12,hwupload"
                venc = ["-c:v", "h264_vaapi"]
            else:  # libx264
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = [ffmpeg] + pre + ["-loop", "1", "-framerate", str(fps), "-i", in_path]
            if audio_path:
                cmd += ["-i", audio_path]
            cmd += ["-vf", vf] + venc + ["-pix_fmt", "yuv420p", "-r", str(fps)]
            if audio_path:
                cmd += ["-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE]
            cmd += (["-t", f"{dur:.3f}", "-shortest"] if audio_path else ["-t", f"{dur:.3f}"])
            cmd += ["-movflags", "+faststart", "-y", out_path]
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"motion encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"motion video failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _motion_existing_video(video_data: bytes, source_filename: str, render_fn) -> bytes:
    """Apply a still-image motion (`render_fn`) to an existing static-frame effect
    video, keeping its audio: pulls the first frame + the original audio and
    re-renders the motion over the clip's real length. Returns MP4 bytes."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    tmp_dir = tempfile.mkdtemp(prefix="media_motionv_")
    vin = os.path.join(tmp_dir, "in.mp4")
    frame = os.path.join(tmp_dir, "frame.png")
    try:
        with open(vin, "wb") as f:
            f.write(video_data)
        dur = _probe_duration(vin) or 5.0
        extract = subprocess.run(
            [ffmpeg, "-i", vin, "-frames:v", "1", "-y", frame],
            capture_output=True, timeout=120, text=True,
        )
        if extract.returncode != 0 or not os.path.exists(frame):
            raise RuntimeError(f"could not extract a frame: {(extract.stderr or '')[-200:]}")
        with open(frame, "rb") as f:
            frame_data = f.read()
        return render_fn(frame_data, "frame.png", duration=dur, audio_path=vin)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _zoom_vf(W: int, H: int, n_frames: int) -> str:
    # Pre-upscale 2x so zoompan's integer steps don't jitter, then zoom from
    # 1.25x down to 1.0 over the clip, kept centred.
    zexpr = f"max(1.0,1.25-0.25*on/{n_frames})"
    return (
        f"scale={2 * W}:{2 * H},"
        f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={n_frames}:s={W}x{H}:fps=25"
    )


def _shake_vf(W: int, H: int, n_frames: int) -> str:
    # Camera shake: upscale ~10% for margin, then crop a W×H window whose centre
    # jitters via two out-of-phase sinusoids per axis (stays inside the margin).
    sw = max(W + 2, int(W * 1.1) // 2 * 2)
    sh = max(H + 2, int(H * 1.1) // 2 * 2)
    xexpr = f"(in_w-{W})/2+(in_w-{W})/2*0.9*sin(2*PI*9*t)"
    yexpr = f"(in_h-{H})/2+(in_h-{H})/2*0.9*cos(2*PI*11*t)"
    return f"scale={sw}:{sh},crop={W}:{H}:'{xexpr}':'{yexpr}'"


def _shake_medium_vf(W: int, H: int, n_frames: int) -> str:
    # Gentler shake than `_shake_vf`: smaller margin, lower amplitude/frequency
    # for a subtler, less frenetic wobble.
    sw = max(W + 2, int(W * 1.06) // 2 * 2)
    sh = max(H + 2, int(H * 1.06) // 2 * 2)
    xexpr = f"(in_w-{W})/2+(in_w-{W})/2*0.55*sin(2*PI*6*t)"
    yexpr = f"(in_h-{H})/2+(in_h-{H})/2*0.55*cos(2*PI*7*t)"
    return f"scale={sw}:{sh},crop={W}:{H}:'{xexpr}':'{yexpr}'"


def _pulse_vf(W: int, H: int, n_frames: int) -> str:
    # Rhythmic zoom in/out (bass-thump pulse): the zoom oscillates ~1.0..1.24
    # twice a second, kept centred. Pre-upscale 2x so zoompan's integer steps
    # don't jitter (same trick as `_zoom_vf`).
    zexpr = "1.12+0.12*sin(2*PI*2*on/25)"
    return (
        f"scale={2 * W}:{2 * H},"
        f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={n_frames}:s={W}x{H}:fps=25"
    )


# Trippy colour cycle: rotate the hue a full turn every ~3s while the saturation
# pulses. Shared by the still-image renderer (`_trippy_vf`) and the video recolour
# pass (`recolor_existing_video`) so the look is identical whether trippy is used
# alone or layered over a zoom/shake/pulse motion.
_TRIPPY_HUE = "hue=h='mod(t*120,360)':s='1.4+0.6*sin(2*PI*t*0.5)'"


def _trippy_vf(W: int, H: int, n_frames: int) -> str:
    # Psychedelic colour cycle on a still image (no camera motion). Scale to even
    # dims first (yuv420p requires even W/H).
    return f"scale={W}:{H},{_TRIPPY_HUE}"


def _shake_begin_vf(W: int, H: int, n_frames: int) -> str:
    # Strong shake that decays to still: same jitter as `_shake_vf` multiplied by
    # a linear envelope that reaches 0 at the first third of the clip, so it
    # shakes hard at the start then settles for the remainder.
    dur = max(0.5, n_frames / 25.0)
    env = f"max(0,1-3*t/{dur:.3f})"
    sw = max(W + 2, int(W * 1.1) // 2 * 2)
    sh = max(H + 2, int(H * 1.1) // 2 * 2)
    xexpr = f"(in_w-{W})/2+(in_w-{W})/2*0.9*{env}*sin(2*PI*9*t)"
    yexpr = f"(in_h-{H})/2+(in_h-{H})/2*0.9*{env}*cos(2*PI*11*t)"
    return f"scale={sw}:{sh},crop={W}:{H}:'{xexpr}':'{yexpr}'"


def image_zoompan_video(image_data: bytes, source_filename: str, duration: float = 4.0,
                        audio_path: Optional[str] = None) -> bytes:
    """Ken Burns zoom-out from a still image (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _zoom_vf, duration, audio_path)


def image_shake_video(image_data: bytes, source_filename: str, duration: float = 4.0,
                      audio_path: Optional[str] = None) -> bytes:
    """Camera-shake clip from a still image (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _shake_vf, duration, audio_path)


def image_medshake_video(image_data: bytes, source_filename: str, duration: float = 4.0,
                         audio_path: Optional[str] = None) -> bytes:
    """Gentler camera-shake clip from a still image (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _shake_medium_vf, duration, audio_path)


def image_beginshake_video(image_data: bytes, source_filename: str, duration: float = 4.0,
                           audio_path: Optional[str] = None) -> bytes:
    """Camera-shake clip that shakes hard at the start then settles (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _shake_begin_vf, duration, audio_path)


def image_trippy_video(image_data: bytes, source_filename: str, duration: float = 4.0,
                       audio_path: Optional[str] = None) -> bytes:
    """Psychedelic hue-cycling clip from a still image (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _trippy_vf, duration, audio_path)


def image_pulse_video(image_data: bytes, source_filename: str, duration: float = 4.0,
                      audio_path: Optional[str] = None) -> bytes:
    """Rhythmic zoom-pulse clip from a still image (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _pulse_vf, duration, audio_path)


def _glow_vf(W: int, H: int, n_frames: int) -> str:
    """Generic "glow" enhancer: a gentle breathing zoom + a colour pop + a soft light
    sweep travelling diagonally across the frame — makes a still photo feel alive and
    stand out, without committing to a specific gag effect. The working resolution is
    capped (long edge 1280) so the per-pixel `geq` light sweep stays cheap even on big
    phone photos; the output is small and broadly playable."""
    cap = 1280
    if max(W, H) > cap:
        if W >= H:
            Wt, Ht = cap, int(round(H * cap / W))
        else:
            Wt, Ht = int(round(W * cap / H)), cap
    else:
        Wt, Ht = W, H
    Wt = max(2, (Wt // 2) * 2)
    Ht = max(2, (Ht // 2) * 2)
    dur = max(0.5, n_frames / 25.0)
    # Breathing Ken Burns: zoom oscillates 1.0..1.02 over a ~6s cycle, kept centred (very gentle).
    # Pre-upscale 2x so zoompan's integer steps don't jitter (same trick as `_zoom_vf`).
    zexpr = "1.01+0.01*sin(2*PI*on/(25*6))"
    zp = (f"scale={2 * Wt}:{2 * Ht},"
          f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={n_frames}:s={Wt}x{Ht}:fps=25")
    # Colour pop + crispen (luma-only unsharp so chroma doesn't get noisy).
    pop = "eq=contrast=1.035:saturation=1.10:brightness=0.012,unsharp=5:5:0.4:5:5:0.0"
    # Soft light sweep: add a gaussian luma band that crosses the frame once over the
    # clip, its centre `cpos` sliding -0.2..1.2 along the normalised TL->BR diagonal.
    # (Single-quoted expressions protect the commas inside pow()/clip() from the
    # filtergraph parser — same as the zoompan z='max(1.0,...)' expressions above.)
    cpos = f"(-0.2+1.4*T/{dur:.3f})"
    band = f"exp(-pow(((X/W+Y/H)/2-{cpos})/0.16,2))"
    sweep = (f"geq=lum='clip(lum(X,Y)+40*{band},0,255)':"
             f"cb='cb(X,Y)':cr='cr(X,Y)'")
    # Gentle vignette so the glow reads on ANY background (a pure-white photo shows
    # nothing from the additive sweep/saturation alone — the darkened edges give it depth).
    return f"{zp},{pop},vignette=PI/12,{sweep}"


def image_glow_video(image_data: bytes, source_filename: str, duration: float = 5.0,
                     audio_path: Optional[str] = None) -> bytes:
    """Generic "glow" enhancement clip from a still image (see `_render_motion_video`)."""
    return _render_motion_video(image_data, source_filename, _glow_vf, duration, audio_path)


def _glow_overlay_vf(W: int, H: int, dur: float) -> str:
    """The glow LOOK (colour pop + a soft light sweep) WITHOUT the breathing zoom — for
    layering over an existing clip's real frames (e.g. `alive glow`) so the underlying
    motion/parallax is kept, the same way `trippy` recolours frames without freezing."""
    pop = "eq=contrast=1.035:saturation=1.10:brightness=0.012,unsharp=5:5:0.4:5:5:0.0"
    cpos = f"(-0.2+1.4*T/{max(0.5, dur):.3f})"
    band = f"exp(-pow(((X/W+Y/H)/2-{cpos})/0.16,2))"
    sweep = f"geq=lum='clip(lum(X,Y)+40*{band},0,255)':cb='cb(X,Y)':cr='cr(X,Y)'"
    return f"{pop},vignette=PI/12,{sweep}"


def glow_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Apply the glow look (colour pop + light sweep) OVER the real frames of an effect
    video, keeping its motion + audio — lets `glow` compose on top of alive/zoom/etc."""
    return _motion_filter_video(video_data, source_filename,
                                lambda W, H, dur, n: _glow_overlay_vf(W, H, dur))


def zoom_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Apply the zoom-out camera move OVER the real frames of an effect video, keeping
    its motion (parallax/animation) + audio — no longer freezes to the first frame."""
    return _motion_filter_video(video_data, source_filename,
                                lambda W, H, dur, n: _zoom_vf_video(W, H, dur))


def shake_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Apply the camera-shake over the real frames, keeping motion + audio."""
    return _motion_filter_video(video_data, source_filename,
                                lambda W, H, dur, n: _shake_vf(W, H, n))


def medshake_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Apply the gentler camera-shake over the real frames, keeping motion + audio."""
    return _motion_filter_video(video_data, source_filename,
                                lambda W, H, dur, n: _shake_medium_vf(W, H, n))


def beginshake_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Apply the shake-then-settle over the real frames, keeping motion + audio."""
    return _motion_filter_video(video_data, source_filename,
                                lambda W, H, dur, n: _shake_begin_vf(W, H, n))


def pulse_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Apply the rhythmic zoom-pulse over the real frames, keeping motion + audio."""
    return _motion_filter_video(video_data, source_filename,
                                lambda W, H, dur, n: _pulse_vf_video(W, H, dur))


def recolor_existing_video(video_data: bytes, source_filename: str = "video.mp4") -> bytes:
    """Hue-cycle EVERY frame of an existing video (trippy colours) while keeping its
    motion and audio. Unlike `_motion_existing_video` (which freezes to one frame),
    this re-encodes the real frames — so `trippy` can layer over a zoom/shake/pulse
    clip without killing the motion. Reuses the HW-accel encoder autodetect."""
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    tmp_dir = tempfile.mkdtemp(prefix="media_recolor_")
    in_suffix = _ext(source_filename) or ".mp4"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(video_data)

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre, vf = [], _TRIPPY_HUE + ",format=yuv420p"
            if encoder == "h264_nvenc":
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            elif encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                vf = _TRIPPY_HUE + ",format=nv12,hwupload"
                venc = ["-c:v", "h264_vaapi"]
            else:  # libx264
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = ([ffmpeg] + pre + ["-i", in_path, "-vf", vf] + venc
                   + ["-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-y", out_path])
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"recolor encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"recolor video failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _probe_video_wh(path: str) -> Tuple[int, int]:
    """First video stream's (width, height) via ffprobe; (1280, 720) on failure."""
    ffmpeg = resolve_ffmpeg()
    ffprobe = ffmpeg[:-6] + "ffprobe" if ffmpeg.endswith("ffmpeg") else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, timeout=60, text=True,
        )
        w, h = (out.stdout or "").strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1280, 720


# Video-stream versions of the zoom/pulse camera moves. The still-image builders run
# `zoompan` with d=n_frames (spinning frames out of ONE picture); over a real video we
# use the same zoompan but with `d=1` (one output frame per input frame) so the clip's
# own motion is preserved while the zoom animates via the per-output-frame index `on`.
# (A plain `crop`/`scale` can't animate size — ffmpeg fixes those at config time.)
def _zoom_vf_video(W: int, H: int, dur: float) -> str:
    # Smooth one-way Ken Burns pull-BACK across the WHOLE clip: start zoomed in (1.30)
    # and ease out to the full frame (1.0), centred — the SAME direction as the still
    # image `zoom` (`_zoom_vf`, 1.25→1.0) so `prayer zoom` matches `dildo zoom`. An
    # earlier sinusoidal version oscillated in/out (~5s period), which read as a "wavey"
    # wobble rather than a zoom; the 30% magnitude stays clearly perceptible even on long
    # 10-18s clips (the old slow 25% ramp that "didn't zoom" was too subtle).
    n = max(1, int(round(25 * dur)))
    z = f"max(1.0,1.30-0.30*on/{n})"
    return (f"scale={2 * W}:{2 * H},"
            f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={W}x{H}:fps=25")


def _pulse_vf_video(W: int, H: int, dur: float) -> str:
    # A zoom-IN pulse crops: at zoom z the frame loses (1 - 1/z) of its width and height, half off
    # each edge. The old 1.00..1.24 threw away 12% of every edge at each peak, and effects that BAKE
    # their own overlays into the frame — the character composites in character.py, the captions in
    # text.py — draw inside that band: a corner character sits at a 2.5-3% margin. So `<effect> pulse`
    # sliced the top off the character's head and clipped the caption, twice a second, while the same
    # overlays applied AFTER the modifier were untouched. That asymmetry is what made it look random.
    #
    # 1.00..1.11 keeps the breathing obvious (it is the rhythm that reads, not the magnitude) and
    # halves the worst-case loss to ~5% an edge. It cannot be removed entirely without either
    # letterboxing at the trough or not zooming in at all — the complete fix is overlays living inside
    # a title-safe margin, which is a composition change, not a filter one.
    z = "1.055+0.055*sin(2*PI*2*on/25)"                # ~1.0..1.11, twice a second
    return (f"scale={2 * W}:{2 * H},"
            f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={W}x{H}:fps=25")


def _motion_filter_video(video_data: bytes, source_filename: str, build_vf) -> bytes:
    """Apply a camera-motion filter OVER the real frames of an existing video, keeping
    its motion and audio. `build_vf(W, H, dur, n_frames)` returns the filter chain (no
    format/hwupload suffix). The replacement for `_motion_existing_video`, which froze
    the clip to its first frame — this keeps the parallax/animation underneath."""
    global _video_encoder_cache
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    tmp_dir = tempfile.mkdtemp(prefix="media_vmotion_")
    in_path = os.path.join(tmp_dir, "in.mp4")
    out_path = os.path.join(tmp_dir, "out.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(video_data)
        W, H = _probe_video_wh(in_path)
        W = max(2, (W // 2) * 2)
        H = max(2, (H // 2) * 2)
        dur = _probe_duration(in_path) or 5.0
        n_frames = max(2, int(round(25 * dur)))
        motion = build_vf(W, H, dur, n_frames)

        candidates = _video_encoder_candidates(ffmpeg)
        if _video_encoder_cache and _video_encoder_cache in candidates:
            candidates = [_video_encoder_cache] + [c for c in candidates if c != _video_encoder_cache]

        last_err = ""
        for encoder in candidates:
            pre, vf = [], motion + ",format=yuv420p"
            if encoder == "h264_nvenc":
                venc = ["-c:v", "h264_nvenc", "-preset", "p5"]
            elif encoder == "h264_vaapi":
                pre = ["-vaapi_device", _render_node()]
                vf = motion + ",format=nv12,hwupload"
                venc = ["-c:v", "h264_vaapi"]
            else:  # libx264
                venc = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
            cmd = ([ffmpeg] + pre + ["-i", in_path, "-vf", vf] + venc
                   + ["-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-y", out_path])
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                _video_encoder_cache = encoder
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (result.stderr or "")[-300:]
            logger.warning(f"video-motion encoder {encoder} failed, trying next: {last_err}")
            if os.path.exists(out_path):
                os.unlink(out_path)

        raise RuntimeError(f"video-motion failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Image <-> PDF conversion
# ---------------------------------------------------------------------------

def images_to_pdf(images: List[Tuple[str, bytes]]) -> bytes:
    """Combine images into a single PDF (one image per page)."""
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    pages = []
    for filename, data in images:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            pages.append(img.copy())

    if not pages:
        raise ValueError("no images to convert")

    out = io.BytesIO()
    pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:])
    return out.getvalue()


def pdf_to_images(pdf_data: bytes, dpi: int = 150, max_pages: int = PDF_MAX_PAGES) -> List[Tuple[str, bytes]]:
    """Render PDF pages to PNG images, capped at `max_pages`.

    Returns [(filename, bytes), ...].
    """
    import fitz  # PyMuPDF

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    outputs: List[Tuple[str, bytes]] = []
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    try:
        page_count = min(doc.page_count, max_pages)
        for page_index in range(page_count):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix)
            outputs.append((f"page_{page_index + 1}.png", pix.tobytes("png")))
    finally:
        doc.close()
    if not outputs:
        raise ValueError("PDF has no pages to convert")
    return outputs


# ---------------------------------------------------------------------------
# High-level attachment processors
# ---------------------------------------------------------------------------

PDF_IMAGE_MAX_DIM = 1600      # embedded images bigger than this are downscaled before re-encoding
PDF_IMAGE_QUALITY = 75


def compress_pdf(data: bytes, image_max_dim: int = PDF_IMAGE_MAX_DIM,
                 quality: int = PDF_IMAGE_QUALITY) -> bytes:
    """Shrink a PDF, keeping it a real PDF (text stays selectable, pages stay pages).

    Two passes, in the order that pays:
      1. re-encode each embedded raster image as a JPEG, downscaled to `image_max_dim` on its long side —
         this is where the bytes almost always are (a scan or a slide deck is really just big images in a
         PDF wrapper);
      2. structural cleanup — drop orphaned objects and deflate what's left.

    We deliberately do NOT rasterize the pages. That compresses beautifully and destroys the document: the
    text layer, selection and search all go with it. An image whose re-encode comes out bigger (already
    optimized, or tiny) is left untouched, and images with a soft mask are skipped entirely — JPEG has no
    alpha channel, so "compressing" them would silently drop their transparency.
    """
    import fitz  # PyMuPDF
    from PIL import Image

    def _transparent(xref: int) -> bool:
        """True if this image carries transparency we'd destroy by re-encoding it as an opaque JPEG.

        `get_images()` only reports the /SMask, so a stencil /Mask, a colour-key mask, an /ImageMask or an
        inverted /Decode array all slip past that check — and those are exactly the cut-out logos and stamps
        in letterheads and slide decks. Flattening one turns its transparent regions into solid blocks that
        cover the text underneath, which is a corrupted document, not a compressed one. When in doubt (the
        lookup fails), leave the image alone.
        """
        for key in ("SMask", "Mask", "ImageMask", "Decode"):
            try:
                kind, _ = doc.xref_get_key(xref, key)
            except Exception:
                return True
            if kind and kind != "null":
                return True
        return False

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        seen: set = set()
        for page in doc:
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in seen:
                    continue
                seen.add(xref)
                if _transparent(xref):
                    continue
                try:
                    raw = (doc.extract_image(xref) or {}).get("image") or b""
                    if not raw:
                        continue
                    img = Image.open(io.BytesIO(raw))
                    img.load()
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    if max(img.size) > image_max_dim:
                        img.thumbnail((image_max_dim, image_max_dim), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
                    new = buf.getvalue()
                    if len(new) < len(raw):
                        page.replace_image(xref, stream=new)
                except Exception as e:
                    logger.debug(f"compress_pdf: skipping image xref {xref}: {e}")
        return doc.tobytes(garbage=4, deflate=True, deflate_images=True,
                           deflate_fonts=True, clean=True)
    finally:
        doc.close()


def _smaller_output(
    orig_name: str, orig_data: bytes, orig_ct: str,
    new_data: bytes, new_name: str, new_ct: str,
) -> OutputFile:
    """Return whichever of original/compressed is smaller as an output file.

    Re-encoding an already-optimized file can grow it; in that case we hand back
    the untouched original so `compress` never makes a file bigger.
    """
    if len(new_data) < len(orig_data):
        return {"filename": new_name, "data": new_data, "content_type": new_ct}
    return {
        "filename": orig_name,
        "data": orig_data,
        "content_type": orig_ct or new_ct,
    }


EFFECT_VIDEO_COMPRESS_THRESHOLD = 3_000_000  # bytes; bigger effect videos get compressed
# Images get a lower bar: a stamp/meme on a 12 MP phone photo comes back as a several-MB JPEG at
# a resolution nothing displays, and unlike a video it is cheap to re-encode.
EFFECT_IMAGE_COMPRESS_THRESHOLD = 1_200_000


def _has_alpha(data: bytes) -> bool:
    """True if these image bytes carry transparency (so re-encoding to JPEG would destroy it).
    Errs on the side of True: an unreadable image is left alone rather than flattened."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.mode in ("RGBA", "LA", "PA") or "transparency" in (im.info or {})
    except Exception:
        return True


def compress_effect_outputs(outputs: List[OutputFile],
                            video_threshold: int = EFFECT_VIDEO_COMPRESS_THRESHOLD,
                            image_threshold: int = EFFECT_IMAGE_COMPRESS_THRESHOLD) -> List[OutputFile]:
    """Auto-compress rendered outputs on their way back to the user: videos through
    `compress_video` and images through `compress_image` — the same passes the `compress`
    command runs. An effect on a modern phone photo otherwise hands back a ~10 MB clip or a
    12 MP JPEG, which then has to travel to Telegram/Blossom/the fediverse.

    Only files OVER the per-type threshold are touched, a result that isn't actually smaller
    is discarded, and any failure keeps the original — compression must never cost the user
    their render. Shared by the web/Telegram command path, the fedi-bot media_api path and
    the Meme Builder, so every interface delivers the same thing."""
    result: List[OutputFile] = []
    for f in outputs or []:
        data = f.get("data")
        ct = (f.get("content_type") or "").lower()
        name = f.get("filename", "file")
        try:
            if ct.startswith("video/") and data and len(data) > video_threshold:
                out = compress_video(data, name)
            elif ct.startswith("image/") and data and len(data) > image_threshold:
                # compress_image re-encodes as JPEG, which is wrong for two kinds of output: a GIF
                # (an animation — it would become one frame) and anything with an alpha channel (a
                # sticker/overlay would gain a white background).
                out = None if (ct.endswith("/gif") or _has_alpha(data)) else compress_image(data)
            else:
                out = None
            if out and len(out) < len(data):
                logger.info("[effects] compressed %s: %d → %d bytes", name, len(data), len(out))
                f = {**f, "data": out}
                if ct.startswith("image/") and not ct.endswith("/jpeg"):
                    # compress_image ALWAYS emits JPEG, so the name and the declared type have to
                    # follow the bytes. Leaving a JPEG called "x.png"/image/png is not cosmetic: the
                    # AI-chat client fetches an artifact and wraps it in a Blob with that declared
                    # type, and a blob: URL is never content-sniffed — the picture just fails to
                    # display (while a direct link, which is sniffed, still works).
                    f["filename"] = os.path.splitext(name)[0] + ".jpg"
                    f["content_type"] = "image/jpeg"
        except Exception as e:
            logger.warning("effect output compress failed for %s, sending original: %s", name, e)
        result.append(f)
    return result


def compress_attachments(attachments: List[Tuple[str, bytes, str]]) -> Tuple[List[OutputFile], str]:
    """Compress each image/video attachment.

    Returns (output_files, summary_text). Files that can't be compressed are
    skipped and noted in the summary.
    """
    outputs: List[OutputFile] = []
    notes: List[str] = []

    for filename, data, content_type in attachments:
        stem = Path(filename).stem or "file"
        original = len(data)
        try:
            if is_animated_gif(filename, data, content_type):
                # Animated GIF → MP4 (H.264): keeps the animation and shrinks hugely.
                # Treating it as a still image would flatten it to one frame.
                compressed = compress_video(data, filename)
                out = _smaller_output(
                    filename, data, content_type, compressed,
                    f"{stem}_compressed.mp4", "video/mp4",
                )
                outputs.append(out)
                notes.append(f"🎬 {filename} (animated GIF → MP4): {_human_size(original)} → {_human_size(len(out['data']))}")
            elif is_image(filename, content_type):
                compressed = compress_image(data)
                out = _smaller_output(
                    filename, data, content_type, compressed,
                    f"{stem}_compressed.jpg", "image/jpeg",
                )
                outputs.append(out)
                notes.append(f"🖼️ {filename}: {_human_size(original)} → {_human_size(len(out['data']))}")
            elif is_video(filename, content_type):
                compressed = compress_video(data, filename)
                out = _smaller_output(
                    filename, data, content_type, compressed,
                    f"{stem}_compressed.mp4", "video/mp4",
                )
                outputs.append(out)
                notes.append(f"🎬 {filename}: {_human_size(original)} → {_human_size(len(out['data']))}")
            elif is_pdf(filename, content_type):
                compressed = compress_pdf(data)
                out = _smaller_output(
                    filename, data, content_type, compressed,
                    f"{stem}_compressed.pdf", "application/pdf",
                )
                outputs.append(out)
                notes.append(f"📄 {filename}: {_human_size(original)} → {_human_size(len(out['data']))}")
            else:
                notes.append(f"⏭️ {filename}: not an image, video or PDF, skipped")
        except Exception as e:
            logger.error(f"compress failed for {filename}: {e}", exc_info=True)
            notes.append(f"❌ {filename}: {e}")

    summary = "## 🗜️ Compression\n\n" + "\n".join(notes) if notes else "No files to compress."
    return outputs, summary


def remove_background_attachments(attachments: List[Tuple[str, bytes, str]]) -> Tuple[List[OutputFile], str]:
    """Remove the background from each attached IMAGE, returning a transparent PNG.

    Uses rembg (u2net ONNX via onnxruntime; the ~170MB model auto-downloads to ~/.u2net on first
    use). Non-image attachments are skipped. Returns (output_files, summary_text)."""
    outputs: List[OutputFile] = []
    notes: List[str] = []
    try:
        from rembg import remove as _rembg_remove
        from PIL import Image
    except Exception as e:
        return [], f"Background removal isn't available — `rembg` not installed ({e}). Install it (requirements.txt)."
    for filename, data, content_type in attachments:
        if not is_image(filename, content_type):
            notes.append(f"⏭️ {filename}: not an image — skipped")
            continue
        stem = Path(filename).stem or "image"
        try:
            cut = _rembg_remove(data)  # rembg returns a PNG with the background cut out
            # Force a real RGBA alpha channel + re-encode as PNG, so the background is
            # genuinely transparent regardless of the rembg session's default output mode.
            with Image.open(io.BytesIO(cut)) as _im:
                _rgba = _im.convert("RGBA")
            _buf = io.BytesIO()
            _rgba.save(_buf, format="PNG")
            cut = _buf.getvalue()
            outputs.append({"filename": f"{stem}_nobg.png", "data": cut, "content_type": "image/png"})
            notes.append(f"✂️ {filename}: background removed → transparent {stem}_nobg.png")
        except Exception as e:
            logger.warning(f"remove_background failed for {filename}: {e}", exc_info=True)
            notes.append(f"❌ {filename}: background removal failed ({e})")
    summary = "## ✂️ Background Removal\n\n" + "\n".join(notes) if notes else "Attach an image to remove its background."
    return outputs, summary


def convert_attachments(
    attachments: List[Tuple[str, bytes, str]],
    target: str = "",
) -> Tuple[List[OutputFile], str]:
    """Convert images <-> PDF.

    Direction is inferred from the attachments: PDFs become images, images
    become a single PDF. `target` is an optional hint ("pdf" or "images") used
    only to disambiguate when both are present.
    """
    images = [(fn, data) for fn, data, ct in attachments if is_image(fn, ct)]
    pdfs = [(fn, data) for fn, data, ct in attachments if is_pdf(fn, ct)]

    target = (target or "").strip().lower()
    outputs: List[OutputFile] = []

    # Decide direction.
    want_pdf = target in ("pdf", "to pdf")
    want_images = target in ("image", "images", "img", "png", "jpg", "to images")

    if pdfs and not want_pdf and (want_images or not images):
        # PDF -> images
        notes: List[str] = []
        for filename, data in pdfs:
            stem = Path(filename).stem or "document"
            try:
                pages = pdf_to_images(data)
                for page_name, page_bytes in pages:
                    outputs.append({
                        "filename": f"{stem}_{page_name}",
                        "data": page_bytes,
                        "content_type": "image/png",
                    })
                note = f"📄 {filename}: {len(pages)} page(s) → PNG"
                if len(pages) >= PDF_MAX_PAGES:
                    note += f" (capped at first {PDF_MAX_PAGES})"
                notes.append(note)
            except Exception as e:
                logger.error(f"PDF->image failed for {filename}: {e}", exc_info=True)
                notes.append(f"❌ {filename}: {e}")
        summary = "## 🔄 Convert (PDF → images)\n\n" + "\n".join(notes)
        return outputs, summary

    if images:
        # images -> single PDF
        try:
            pdf_bytes = images_to_pdf(images)
            outputs.append({
                "filename": "converted.pdf",
                "data": pdf_bytes,
                "content_type": "application/pdf",
            })
            names = ", ".join(fn for fn, _ in images[:5])
            summary = (
                f"## 🔄 Convert (images → PDF)\n\n"
                f"Combined {len(images)} image(s) into `converted.pdf` ({names})."
            )
            if pdfs:
                # Both images and PDFs were attached; we made a PDF from the images.
                summary += (
                    f"\n\n_Ignored {len(pdfs)} PDF(s) — send `convert images` or `convert pdf` "
                    f"to pick a direction when mixing types._"
                )
            return outputs, summary
        except Exception as e:
            logger.error(f"image->PDF failed: {e}", exc_info=True)
            return [], f"❌ Could not convert images to PDF: {e}"

    return [], (
        "Nothing to convert. Attach image(s) to make a PDF, or a PDF to extract images."
    )
