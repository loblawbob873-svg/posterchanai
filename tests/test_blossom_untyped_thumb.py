"""An UNTYPED blob is still a photograph.

`new File([bytes], 'holiday.jpg')` has `type === ''` — the File constructor does not look at the
name, and nothing downstream recovers it. So every client path that rebuilds a file out of raw bytes
without passing a type explicitly uploaded real media as `application/octet-stream`.

That is invisible until something keys a decision on the stored MIME, and two things do:

  - the drive's card chooser (blobThumb) drew a generic 📎 and never even requested a preview;
  - the server's thumbnail branch was gated on `mime.startswith("image/"|"video/")`, so even when
    asked it served the WHOLE FILE to an <img> — which renders for an image (full size, full
    bandwidth) and cannot be decoded at all for a video.

Reported as "copied a file from a Synced Folder to Documents and now it has no thumbnail". The write
path is fixed, but a blob is immutable and content-addressed: the ones already stored keep the wrong
type for ever, so the read path has to cope. The bytes are the one source that cannot be wrong.

These tests run the real magic-number table over real file headers rather than asserting on source
text, because the failure mode is a header that is not recognised, not a line that is missing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import blossom_service as bs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Real leading bytes. Only the first 16 are ever read, which is the whole point — the gate must not
# cost a full download to answer.
HEADS = {
    "png":  b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    "jpg":  b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00",
    "gif":  b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff",
    "webp": b"RIFF\x24\x00\x00\x00WEBPVP8 ",
    "mp4":  b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00",
    "webm": b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f",
    "pdf":  b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
    "zip":  b"PK\x03\x04\x14\x00\x00\x00\x08\x00\x00\x00\x00\x00",
}


def test_image_bytes_sniff_as_an_image_whatever_the_stored_type_says():
    for ext in ("png", "jpg", "gif", "webp"):
        got = bs.sniff_mime(HEADS[ext])
        assert got.startswith("image/"), f"{ext}: sniffed {got!r} — a photo would draw a paperclip"


def test_video_bytes_sniff_as_a_video():
    for ext in ("mp4", "webm"):
        got = bs.sniff_mime(HEADS[ext])
        assert got.startswith("video/"), f"{ext}: sniffed {got!r} — no frame would be extracted"


def test_documents_do_not_sniff_as_media():
    """The negative half matters as much: a pdf or a zip that claimed to be media would have its
    whole body read and handed to ffmpeg on every render."""
    for ext in ("pdf", "zip"):
        got = bs.sniff_mime(HEADS[ext])
        assert got and not got.startswith(("image/", "video/")), f"{ext}: sniffed {got!r}"


def test_unrecognised_bytes_sniff_as_nothing():
    assert bs.sniff_mime(b"not a file header") == ""
    assert bs.sniff_mime(b"") == ""


def test_sniff_needs_no_more_than_sixteen_bytes():
    """The router slices data[:16]. If the table ever needed more, the gate would silently start
    answering '' for the formats that need it — i.e. quietly stop working."""
    for ext, head in HEADS.items():
        assert bs.sniff_mime(head[:16]) == bs.sniff_mime(head), f"{ext} needs more than 16 bytes"


def test_sniff_mime_agrees_with_sniff_ext():
    """Two tables that can disagree eventually do. sniff_mime is defined in terms of sniff_ext, and
    every extension that one produces must map in the other or the gate has a silent hole."""
    for head in HEADS.values():
        assert bs.sniff_ext(head), "sniff_ext lost a format sniff_mime is tested on"
        assert bs.sniff_mime(head), "sniff_ext knows this format and sniff_mime does not"


# ---- the gate that consumes it ------------------------------------------------------------------

def test_thumb_branch_admits_untyped_blobs():
    src = open(os.path.join(ROOT, "app", "routers", "blossom.py"), encoding="utf-8").read()
    assert '_generic = (not blob.mime) or mime.startswith("application/octet-stream")' in src, \
        "the thumb branch must let an untyped blob in — gated on the stored MIME it never could"
    assert "blossom_service.sniff_mime(await _read_head(db, blob)) or mime" in src, \
        "an untyped blob's kind must come from its bytes, and only when the stored type is useless"


def test_the_sniff_reads_sixteen_bytes_and_not_the_blob():
    """THE REGRESSION THIS FEATURE NEARLY SHIPPED.

    Sniffing after `read_full` is free only if every candidate is small, and the untyped class is
    the exact opposite: every encrypted-drive upload, music track and folder-sync manifest is
    application/octet-stream, so it is where the multi-gigabyte blobs live. `/thumb/<sha>` is
    unauthenticated, so one request against a 2 GB `.enc` would buffer 2 GB into the single uvicorn
    worker — enough to OOM a 12 GB node — and a Files grid of them does it once per tile.
    """
    src = open(os.path.join(ROOT, "app", "routers", "blossom.py"), encoding="utf-8").read()
    assert "async def _read_head(" in src, "the 16-byte read must be a named, shared helper"
    # The sniff must be decided BEFORE the branch that calls read_full.
    gate = src.index('if force_thumb or request.query_params.get("thumb"):')
    full = src.index("data = await blossom_service.read_full(db, blob)")
    sniff = src.index("blossom_service.sniff_mime(await _read_head(db, blob))")
    assert gate < sniff < full, "the kind must be decided from 16 bytes before the blob is buffered"
    assert "sniff_mime(data[:16])" not in src, "that sniff runs only after the whole blob is in RAM"


def test_an_unsniffable_blob_is_not_given_a_permanent_no_thumbnail_sentinel():
    """sniff_ext knows 13 magic numbers; bmp/tiff/ico/svg fall through it.

    Treating "the table does not know this" as "there is no thumbnail" wrote an empty-bytes sentinel
    to DISK — permanent, surviving any later fix — for files PIL decodes perfectly well, and the
    client then drew a broken image where it used to draw a paperclip. Falling out of the branch
    serves the original bytes instead, which is exactly what these blobs did before the feature.
    """
    src = open(os.path.join(ROOT, "app", "routers", "blossom.py"), encoding="utf-8").read()
    branch = src[src.index('if force_thumb or request.query_params.get("thumb"):'):src.index("# HTTP Range")]
    # The gate itself is the guarantee: only a kind we RECOGNISED as media enters the branch, so an
    # unsniffable blob is never in a position to have a sentinel written for it.
    assert 'if (_tmime.startswith("image/") or _tmime.startswith("video/")) \\' in branch, \
        "generation must be entered only for a kind actually recognised as media"
    # The one remaining negative sentinel is the video decode failing, which is a real answer about
    # a real video. There must be no unconditional one for 'the table did not know this format'.
    assert branch.count('_thumb_put(sha, b"")') == 0, \
        "an unrecognised blob must fall OUT of the branch, never into a permanent on-disk sentinel"
    assert branch.count("_thumb_put(") == 2, \
        "exactly two writes: the video sentinel-or-frame, and the image thumbnail"


def test_client_asks_for_a_preview_when_the_name_knows_better():
    """The server can only answer what it is asked. blobThumb decides the card AND whether a preview
    URL is requested at all, so both halves are needed and they have to agree."""
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    assert "if(!t || /^application\\/octet-stream/i.test(t)){ const g = ext && mimeForName('x.' + ext); if(g) t = g; }" in src, \
        "blobThumb must fall back to the extension for an untyped blob"
    assert "function mimeForName(" in src and "const _EXT_MIME" in src


def test_the_extension_is_resolved_before_the_type_is_consulted():
    """Deriving `ext` from the MIME first turns application/octet-stream into 'octet-stre', which
    matches nothing — so the fallback silently could not fire for any caller that omits `ext`. The
    compose/DM attach picker calls `blobThumb(b)` with one argument, and showed every untyped photo
    as an identical anonymous tile."""
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    fn = src[src.index("function blobThumb(b, ext){"):]
    fn = fn[:fn.index("\n  }")]
    assert fn.index("if(!ext) ext = extOfBlob(b);") < fn.index("mimeForName("), \
        "ext must come from the blob before the MIME is used as a source for it"
    assert fn.index("mimeForName(") < fn.index("ext=(ext||("), \
        "the type fallback has to run before ext is defaulted from that same type"


def test_image_tiles_have_an_error_fallback():
    """A guessed type can be wrong, and a server that cannot preview answers 404 with a day of
    cache. Without a fallback the card shows the browser's broken-image glyph for that long —
    strictly worse than the paperclip it replaced. The video branch always had one."""
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    assert 'class="ithumb"' in src, "image tiles need a hook to bind an onerror to"
    assert "function _bindThumbFallback(" in src
    assert "$$('.ithumb',root).forEach(im=> im.onerror=" in src
    # …and bound on BOTH surfaces that render the markup, not just the Files grid.
    assert src.count("_bindThumbFallback(") >= 3, \
        "the attach picker renders the same tiles and needs the same fallback"


def test_the_write_path_stopped_creating_untyped_blobs():
    """Fixing only the read path leaves every future copy relying on a sniff."""
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    assert "function fileFromBytes(" in src
    assert "_keepBytes(fileFromBytes(bytes, name), '', {exact:true})" in src, \
        "the synced-folder copy must carry a type — it is what made this bug"
    assert "_keepBytes(new File([bytes], name), '')" not in src


def test_keeping_a_copy_never_re_encodes_it():
    """THE OTHER HALF OF GIVING THE COPY A TYPE, and it is a data-loss bug.

    uploadBlob runs compressMedia, and compressImage returns early only for a non-image type. With
    `type === ''` the old copy slipped past that guard by accident; naming the type correctly put
    every kept photo through a 2560px canvas re-encode at quality as low as 0.45, stripping EXIF and
    changing the sha256 — while the toast said it had been saved. "Save a copy" is an ARCHIVE.
    """
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    assert "if(!(opts && opts.noCompress)) file=await compressMedia(file);" in src, \
        "uploadBlob must offer a way to store the exact bytes"
    assert "const up = Object.assign({folder:'Posts'}, exact ? {noCompress:true} : null);" in src, \
        "_keepBytes must thread an exact/archival save through to the upload"
    # …and must not hand an exact save to the music library, which TRANSCODES to Opus.
    assert "if(_wantsLibrary(file, kind) && !exact){" in src, \
        "an archival copy must never go through uploadMusicTrack — that re-encodes it"
