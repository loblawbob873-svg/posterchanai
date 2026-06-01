"""
Media Service - Generic image/video compression and image<->PDF conversion.

Provides backend-agnostic helpers used by the Telegram, Matrix and web chat
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
VIDEO_MAX_RESOLUTION = (1280, 720)
VIDEO_AUDIO_BITRATE = '96k'

# Cap how many PDF pages we rasterize at once to bound memory and output count.
PDF_MAX_PAGES = 50

# A single output file produced by a media operation.
OutputFile = dict  # {"filename": str, "data": bytes, "content_type": str}


def _ext(filename: str) -> str:
    return Path(filename or "").suffix.lower()


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


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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

def compress_video(
    data: bytes,
    source_filename: str,
    crf: int = VIDEO_CRF,
    preset: str = VIDEO_PRESET,
    max_resolution: Tuple[int, int] = VIDEO_MAX_RESOLUTION,
) -> bytes:
    """Compress a video with ffmpeg (H.264/AAC, downscaled). Returns MP4 bytes.

    Raises RuntimeError if ffmpeg is unavailable or the transcode fails.
    """
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    if not _ffmpeg_has_libx264(ffmpeg):
        raise RuntimeError(
            "this ffmpeg build has no libx264 (H.264) encoder — install a full "
            "ffmpeg (e.g. the system/Jellyfin build) for video compression"
        )

    tmp_dir = tempfile.mkdtemp(prefix="media_compress_")
    in_suffix = _ext(source_filename) or ".mp4"
    in_path = os.path.join(tmp_dir, f"input{in_suffix}")
    out_path = os.path.join(tmp_dir, "output.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(data)

        max_w, max_h = max_resolution
        # Scale down to fit within max resolution, keep aspect ratio, even dims.
        scale_filter = (
            f"scale='min({max_w},iw)':'min({max_h},ih)':"
            f"force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
        )

        cmd = [
            ffmpeg, '-i', in_path,
            '-vf', scale_filter,
            '-c:v', 'libx264', '-preset', preset, '-crf', str(crf),
            '-c:a', 'aac', '-b:a', VIDEO_AUDIO_BITRATE,
            '-movflags', '+faststart',
            '-y', out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
        if result.returncode != 0 or not os.path.exists(out_path):
            err = (result.stderr or "")[-400:]
            raise RuntimeError(f"video compression failed: {err}")

        with open(out_path, "rb") as f:
            return f.read()
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
            if is_image(filename, content_type):
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
            else:
                notes.append(f"⏭️ {filename}: not an image or video, skipped")
        except Exception as e:
            logger.error(f"compress failed for {filename}: {e}", exc_info=True)
            notes.append(f"❌ {filename}: {e}")

    summary = "## 🗜️ Compression\n\n" + "\n".join(notes) if notes else "No files to compress."
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
